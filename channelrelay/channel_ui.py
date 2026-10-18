# channel_ui.py — 频道代理 Web UI（在 tgfilestream 内核上新增）
# 路由：/ (列表页,鉴权)  /login (POST 密码)  /list (JSON 分页,鉴权)
#       /thumb/{id} (缩略图,鉴权)  /watch/{id}/{name} (播放页,不鉴权)  /s/{id}/{name} (频道流式,Range)
import hashlib, logging, os, time
from typing import cast

from aiohttp import web
from telethon.tl.custom import Message
from telethon.tl.types import (DocumentAttributeVideo,
                               DocumentAttributeAudio, MessageMediaDocument,
                               MessageMediaPhoto)

from .config import tg_channel, ui_password, admin_password
from .telegram import client, transfer
from .util import pack_bit_mask, chat_id_offset, msg_id_offset, get_file_name

log = logging.getLogger(__name__)
routes = web.RouteTableDef()

AUTH_COOKIE = "tgui_auth"
ADMIN_COOKIE = "tgadmin_auth"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 天（未启用；改为会话级 cookie 后浏览器关闭即失效）
thumb_cache: dict = {}
THUMB_CACHE_MAX = 500  # 内存缩略图缓存上限，防无限增长
CHANNELS_FILE = "/app/data/channels.json"
PAGE_SIZE = 30

# 多频道状态：_peers[username] = peer；_peers_by_id[chat_id低32位] = peer
_peers: dict = {}
_peers_by_id: dict = {}
_channel_list: list = []  # [{"username": ..., "title": ..., "active": bool}]
_chan_info_cache: dict = {}  # username -> info


def _load_channels():
    """读频道列表（首次从 TG_CHANNEL 环境变量初始化）。"""
    global _channel_list
    try:
        import json as _json
        if os.path.exists(CHANNELS_FILE):
            with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                _channel_list = _json.load(f) or []
    except Exception as e:
        log.warning(f"channels.json load failed: {e}")
        _channel_list = []
    if not _channel_list and tg_channel:
        _channel_list = [{"username": tg_channel, "title": ""}]
        _save_channels()


def _save_channels():
    import json as _json
    try:
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            _json.dump(_channel_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"channels.json save failed: {e}")


def _channel_names() -> list:
    if not _channel_list:
        _load_channels()
    return [c["username"] for c in _channel_list]


def _channel_from_req(req) -> str:
    """从 query ?c= 取频道；缺省返回空（绝不 fallback，避免串台）。"""
    return req.query.get("c", "").strip()


async def _get_peer(username: str = None):
    """按 username 解析并缓存 peer；同时登记 chat_id 反查表。
    收藏夹（is_self User）返回 User 实体，但取消息时须用 InputPeerSelf——调用方用 _msg_entity()。
    解析失败（频道不存在/无权访问）返回 None，不抛 500。"""
    if not username:
        return None
    if username in _peers:
        return _peers[username]
    try:
        peer = await client.get_entity(username)
    except Exception as e:
        log.warning(f"resolve peer failed: {username}: {e}")
        return None
    _peers[username] = peer
    pid = getattr(peer, "id", 0)
    _peers_by_id[pid & pack_bit_mask] = peer
    # 标题登记
    for c in _channel_list:
        if c["username"] == username and not c.get("title") and getattr(peer, "title", None):
            c["title"] = peer.title
    log.info(f"Channel peer resolved: {username} -> {peer}")
    return peer


def _msg_entity(peer):
    """取消息用实体：收藏夹（is_self User）必须用 'me'（InputPeerSelf）才能按 ID 取到消息。"""
    if peer is not None and getattr(peer, "is_self", False):
        return "me"
    return peer


def _peer_by_packed(packed: int):
    """从 packed id 反查 peer（用于 /thumb /watch /s 等按 chat_id 定位）。"""
    chat_id = (packed >> chat_id_offset) & pack_bit_mask
    return _peers_by_id.get(chat_id)


def _auth_ok(req: web.Request) -> bool:
    """分享密码（访客浏览频道页）。"""
    if not ui_password:
        return True
    ck = req.cookies.get(AUTH_COOKIE, "")
    expect = hashlib.sha256(("tgui:" + ui_password).encode()).hexdigest()
    return ck == expect


def _admin_ok(req: web.Request) -> bool:
    """管理密码（管理页）。"""
    if not admin_password:
        return True
    ck = req.cookies.get(ADMIN_COOKIE, "")
    expect = hashlib.sha256(("tgadmin:" + admin_password).encode()).hexdigest()
    return ck == expect


def _pack(chat_id: int, msg_id: int) -> int:
    # 频道 → channel_bit=0b10；chat_id 通常为负数, 取低 32 位绝对值
    file_id = 0b10 | ((chat_id & pack_bit_mask) << chat_id_offset) | (msg_id << msg_id_offset)
    return file_id





THUMB_DIR = "/app/cache/thumbs"
AVATAR_CACHE = "/app/cache/avatar.jpg"
import os as _os
async def _get_chan_info(username: str = None):
    """频道头部信息：名称、订阅数、头像 URL。按频道缓存。"""
    username = (username or "").strip()
    if not username:
        return {"name": "频道", "subs": "", "avatar": "/avatar", "username": ""}
    if username in _chan_info_cache:
        return _chan_info_cache[username]
    peer = await _get_peer(username)
    if not peer:
        return {"name": username, "subs": "", "avatar": "/avatar?c=" + username, "username": username}
    info = {"name": getattr(peer, "title", None) or getattr(peer, "username", None) or "频道",
            "subs": "", "avatar": "/avatar?c=" + username, "username": username}
    # 订阅者数需要 GetFullChannelRequest
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types import InputPeerChannel
        pid = peer.id if hasattr(peer, "id") else peer
        ph = getattr(peer, "access_hash", 0)
        full = await client(GetFullChannelRequest(InputPeerChannel(pid, ph)))
        cnt = getattr(full.full_chat, "participants_count", None) or 0
        if cnt:
            info["subs"] = f"{cnt:,} 位订阅者"
    except Exception as e:
        log.warning(f"channel info subs failed: {e}")
    _chan_info_cache[username] = info
    return info


@routes.get("/avatar")
async def avatar(req: web.Request) -> web.Response:
    """频道头像（按频道磁盘缓存）。"""
    username = _channel_from_req(req)
    cache_path = _os.path.join(_os.path.dirname(AVATAR_CACHE), f"avatar_{username or 'default'}.jpg")
    peer = await _get_peer(username)
    if not peer:
        return web.Response(status=404, text="no avatar")
    if _os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return web.Response(body=f.read(), content_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400"})
    photo = getattr(peer, "photo", None)
    if not photo:
        return web.Response(status=404, text="no avatar")
    try:
        data = await client.download_profile_photo(peer, file=bytes)
    except Exception as e:
        log.warning(f"avatar failed: {e}")
        return web.Response(status=404, text="no avatar")
    if not data:
        return web.Response(status=404, text="no avatar")
    try:
        with open(cache_path, "wb") as f:
            f.write(data)
    except Exception:
        pass
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


def _thumb_of(media):
    """返回媒体是否有可展示内容：视频带 document.thumbs，照片直接有 photo。"""
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        return bool(doc and getattr(doc, "thumbs", None))
    if isinstance(media, MessageMediaPhoto) or (media and getattr(media, "photo", None)):
        return True
    # 兼容 MessageMediaWebPage 等少见类型，忽略
    return False


def _thumb_kind(media) -> str:
    """'photo'、'video' 或 'text'，决定列表展示/详情方式。"""
    if media is None:
        return "text"
    if isinstance(media, MessageMediaPhoto) or (media and getattr(media, "photo", None)):
        return "photo"
    return "video"


async def _load_thumb_bytes(media, packed: int):
    """从磁盘缓存或 TG 获取缩略图字节（photo→原图；video→最大 thumb）。落盘缓存。
    磁盘文件名用 packed id（含 chat_id+msg_id），避免跨频道同 msg_id 串台。"""
    _os.makedirs(THUMB_DIR, exist_ok=True)
    disk = _os.path.join(THUMB_DIR, f"{packed}.jpg")
    if _os.path.exists(disk):
        with open(disk, "rb") as f:
            return f.read()
    if _thumb_kind(media) == "photo":
        data = await client.download_media(media, file=bytes)
    else:
        data = await client.download_media(media, thumb=-1, file=bytes)
    if not data:
        return None
    try:
        with open(disk, "wb") as f:
            f.write(data)
    except Exception:
        pass
    return data


@routes.get("/c/{channel:[^/]+}")
async def channel_page(req: web.Request) -> web.Response:
    """单频道分享入口：/c/<username> 直接显示该频道消息流（分享给别人的链接）。
    ?tag=xxx 时按标签筛选（服务器端搜索）。"""
    if not _auth_ok(req):
        return web.Response(status=200, content_type="text/html", text=_login_html(req.path_qs))
    username = req.match_info["channel"]
    peer = await _get_peer(username)
    if not peer:
        return web.Response(status=404, text="频道不存在或未解析")
    tag = (req.query.get("tag", "") or "").strip()
    items, next_offset = await _fetch_page_with_groups(peer, username, search=tag)
    msgs = "".join(_render_group(it) if isinstance(it, dict) and "group" in it else _stream_item(it) for it in items)
    chan = await _get_chan_info(username)
    # 单频道分享链接：锁定当前频道，不显示切换条（show_all=False）
    html = render_list_html(msgs, next_offset, peer, chan, username, show_all=False, tag=tag)
    return web.Response(status=200, content_type="text/html", text=html)


@routes.get("/c")
async def channel_all(req: web.Request) -> web.Response:
    """/c（无频道后缀）= 频道总入口：多频道时显示选择页（需分享密码）。"""
    if not _auth_ok(req):
        return web.Response(status=200, content_type="text/html", text=_login_html(req.path_qs))
    names = _channel_names()
    if len(names) == 1:
        return web.Response(status=302, headers={"Location": "/c/" + names[0]})
    html = CHANNEL_PICKER_HTML
    rows = []
    for c in _channel_list:
        rows.append(_picker_card(c))
    html = html.replace("__PICKER_ROWS__", "\n".join(rows))
    return web.Response(status=200, content_type="text/html", text=html)


@routes.get("/manage")
async def manage_page(req: web.Request) -> web.Response:
    """可视化管理页：增删频道、复制分享链接、改密码。管理密码保护。"""
    if not _admin_ok(req):
        return web.Response(status=200, content_type="text/html", text=ADMIN_LOGIN_HTML)
    _load_channels()  # 容器重启后 _channel_list 为空，必须显式加载，否则管理页空白
    html = MANAGE_HTML.replace("__CHANNEL_ROWS__", "\n".join(_manage_row(c) for c in _channel_list))
    added = "[" + ",".join('"%s"' % c["username"] for c in _channel_list) + "]"
    html = html.replace("__ADDED_SET__", added)
    return web.Response(status=200, content_type="text/html", text=html)


@routes.get("/manager")
async def manage_page_alias(req: web.Request) -> web.Response:
    """/manager 别名（用户易记错），302 到 /manage。"""
    return web.Response(status=302, headers={"Location": "/manage"})


def _share_url(username: str) -> str:
    """频道分享链接（公网 URL）。PUBLIC_URL 未配置时退化为相对路径。"""
    if not username:
        return ""
    try:
        from .config import public_url
        base = str(public_url).rstrip("/")
        if base.startswith("http"):
            return f"{base}/c/{username}"
    except Exception:
        pass
    return f"/c/{username}"


def _manage_row(c: dict) -> str:
    import html as _h
    name = _h.escape(c.get("title") or c.get("username") or "")
    user = _h.escape(c.get("username") or "")
    share = _share_url(c.get("username") or "")
    share_esc = share.replace("&", "&amp;").replace("\"", "&quot;")
    return (f'<div class="row">'
            f'<div class="info"><div class="t">{name}</div><div class="u">@{user}</div></div>'
            f'<div class="ops">'
            f'<button class="btn small copy" onclick="copyShare(&quot;{share_esc}&quot;)" title="{share_esc}">🔗 复制分享</button>'
            f'<a href="/c/{user}" target="_blank" class="btn small">打开</a>'
            f'<button class="btn small danger" onclick="delChannel(&quot;{user}&quot;)">删除</button>'
            f'</div></div>')


@routes.get("/manage/api/dialogs")
async def manage_dialogs(req: web.Request) -> web.Response:
    """拉取当前账号可见的所有频道（is_channel），供管理页点选添加。"""
    if not _admin_ok(req):
        return web.Response(status=401, text="unauthorized")
    try:
        dialogs = await client.get_dialogs(limit=200)  # 上限 200，防超大账号拉取卡死
    except Exception as e:
        log.warning(f"get_dialogs failed: {e}")
        return web.json_response({"ok": False, "msg": f"拉取失败: {e}"})
    items = []
    for d in dialogs:
        if not d.is_channel:
            continue
        ent = d.entity
        user = getattr(ent, "username", None) or ""
        is_group = bool(getattr(ent, "megagroup", False))
        items.append({
            "name": getattr(ent, "title", None) or user or f"id:{getattr(ent, 'id', '')}",
            "username": user,
            "id": getattr(ent, "id", 0),
            "kind": "群组" if is_group else "频道",
        })
    items.sort(key=lambda x: (x["kind"], x["name"]))
    return web.json_response({"ok": True, "items": items, "total": len(items)})


@routes.post("/manage/api/add")
async def manage_add(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.Response(status=401, text="unauthorized")
    data = await req.post()
    username = (data.get("username") or "").strip()
    # 兼容输入：@xxx / t.me/xxx / https://t.me/xxx
    username = username.removeprefix("@").removeprefix("https://t.me/").removeprefix("http://t.me/").removeprefix("t.me/").strip()
    if not username:
        return web.json_response({"ok": False, "msg": "频道不能为空"})
    # 校验可解析
    try:
        peer = await client.get_entity(username)
    except Exception as e:
        return web.json_response({"ok": False, "msg": f"无法解析频道: {e}"})
    for c in _channel_list:
        if c["username"] == username:
            return web.json_response({"ok": False, "msg": "频道已存在"})
    _channel_list.append({"username": username, "title": getattr(peer, "title", None) or ""})
    _save_channels()
    _peers[username] = peer
    pid = getattr(peer, "id", 0)
    _peers_by_id[pid & pack_bit_mask] = peer
    return web.json_response({"ok": True, "msg": f"已添加 {username}"})


@routes.post("/manage/api/del")
async def manage_del(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.Response(status=401, text="unauthorized")
    data = await req.post()
    username = (data.get("username") or "").strip()
    _channel_list[:] = [c for c in _channel_list if c["username"] != username]
    _save_channels()
    _peers.pop(username, None)
    return web.json_response({"ok": True, "msg": f"已删除 {username}"})


@routes.post("/manage/api/password")
async def manage_password(req: web.Request) -> web.Response:
    """修改密码：share(分享密码)/admin(管理密码)。写入 /app/data/passwords.json 并更新内存。"""
    if not _admin_ok(req):
        return web.Response(status=401, text="unauthorized")
    data = await req.post()
    kind = (data.get("kind") or "").strip()
    new_pw = (data.get("password") or "").strip()
    if not kind or not new_pw:
        return web.json_response({"ok": False, "msg": "参数不完整"})
    if len(new_pw) < 4:
        return web.json_response({"ok": False, "msg": "密码至少 4 位"})
    import json as _json
    pwf = "/app/data/passwords.json"
    store = {}
    if os.path.exists(pwf):
        try:
            with open(pwf, "r", encoding="utf-8") as f:
                store = _json.load(f)
        except Exception:
            store = {}
    if kind == "share":
        store["ui"] = new_pw
    elif kind == "admin":
        store["admin"] = new_pw
    else:
        return web.json_response({"ok": False, "msg": "类型错误"})
    with open(pwf, "w", encoding="utf-8") as f:
        _json.dump(store, f, ensure_ascii=False, indent=2)
    global ui_password, admin_password
    if kind == "share":
        ui_password = new_pw
    else:
        admin_password = new_pw
    return web.json_response({"ok": True, "msg": "密码已更新，下次生效"})


@routes.get("/")
async def index_page(req: web.Request) -> web.Response:
    # 根路径 = 管理入口：302 到 /manage（管理密码 123321）
    return web.Response(status=302, headers={"Location": "/manage"})


def _picker_card(c: dict) -> str:
    import html as _h
    name = _h.escape(c.get("title") or c.get("username") or "")
    user = _h.escape(c.get("username") or "")
    return (f'<a class="pick" href="/c/{user}">'
            f'<div class="p-avatar"><img src="/avatar?c={user}" '
            "onerror=\"this.style.display='none';this.parentElement.textContent='TV'\"></div>"
            f'<div class="p-info"><div class="p-name">{name}</div><div class="p-user">@{user}</div></div>'
            f'<div class="p-tag"></div></a>')


@routes.get("/list")
async def list_api(req: web.Request) -> web.Response:
    if not _auth_ok(req):
        return web.Response(status=401, text="unauthorized")
    username = _channel_from_req(req)
    if not username:
        return web.json_response({"items": [], "next_offset": 0})
    peer = await _get_peer(username)
    if not peer:
        return web.json_response({"items": [], "next_offset": 0})
    try:
        offset_id = int(req.query.get("offset_id", "0") or 0)
    except ValueError:
        offset_id = 0
    tag = (req.query.get("tag", "") or "").strip()
    items, next_offset = await _fetch_page_with_groups(peer, username, offset_id, search=tag)
    return web.json_response({"items": items, "next_offset": next_offset})


def _has_content(m) -> bool:
    """消息是否值得展示：有媒体（图/视频/文件）或有纯文字。"""
    if not m or not m.message and not m.media:
        return False
    if m.media:
        doc = getattr(m.media, "document", None)
        if doc is not None or getattr(m.media, "photo", None):
            return True
    return bool((m.message or "").strip())


# 标签统计缓存：{username: {"ts": epoch, "tags": {tag: count}}}
_tag_cache: dict = {}
TAG_SCAN = 400   # 扫最近多少条提取标签
TAG_CACHE_TTL = 600  # 10 分钟


async def _tag_stats(peer, username: str) -> dict:
    """统计频道消息中的 #标签 出现次数（扫最近 TAG_SCAN 条，缓存 TTL）。"""
    import re as _re
    now = time.time()
    cached = _tag_cache.get(username)
    if cached and now - cached["ts"] < TAG_CACHE_TTL:
        return cached["tags"]
    counts: dict = {}
    try:
        msgs = await client.get_messages(entity=_msg_entity(peer), limit=TAG_SCAN)
        for m in msgs:
            txt = (m.message or "") if m else ""
            for t in _re.findall(r"#([\w\u4e00-\u9fff][\w\u4e00-\u9fff·\-]*)", txt):
                t = t.strip()
                if t and len(t) <= 24:
                    counts[t] = counts.get(t, 0) + 1
    except Exception as e:
        log.warning(f"tag scan failed: {e}")
    _tag_cache[username] = {"ts": now, "tags": counts}
    return counts


@routes.get("/tags")
async def tags_api(req: web.Request) -> web.Response:
    """频道标签统计：返回 {tags: [{tag, count}], top: 前 N 个}。"""
    if not _auth_ok(req):
        return web.Response(status=401, text="unauthorized")
    username = _channel_from_req(req)
    if not username:
        return web.json_response({"tags": [], "top": []})
    peer = await _get_peer(username)
    if not peer:
        return web.json_response({"tags": [], "top": []})
    counts = await _tag_stats(peer, username)
    # 过滤：只保留出现 >=2 次的高频标签，按次数降序
    top = sorted(((v, k) for k, v in counts.items() if v >= 2), reverse=True)[:20]
    return web.json_response({
        "tags": [{"tag": k, "count": c} for c, k in top],
        "total": len(counts),
    })


@routes.get("/tagsall")
async def tags_all_api(req: web.Request) -> web.Response:
    """全部标签（含低频，按出现次数降序），供标签云页使用。"""
    if not _auth_ok(req):
        return web.Response(status=401, text="unauthorized")
    username = _channel_from_req(req)
    if not username:
        return web.json_response({"tags": []})
    peer = await _get_peer(username)
    if not peer:
        return web.json_response({"tags": []})
    counts = await _tag_stats(peer, username)
    tags = sorted(((v, k) for k, v in counts.items()), reverse=True)
    return web.json_response({"tags": [{"tag": k, "count": c} for c, k in tags]})


@routes.get("/cloud")
async def tag_cloud(req: web.Request) -> web.Response:
    """全标签云页：列出频道所有 #标签（按次数降序），点击进入筛选。"""
    if not _auth_ok(req):
        return web.Response(status=200, content_type="text/html", text=_login_html(req.path_qs))
    username = _channel_from_req(req)
    if not username:
        return web.Response(status=404, text="no channel")
    peer = await _get_peer(username)
    if not peer:
        return web.Response(status=404, text="no peer")
    import urllib.parse as _up
    counts = await _tag_stats(peer, username)
    tags = sorted(((v, k) for k, v in counts.items()), reverse=True)
    u_enc = _up.quote(username)          # URL 编码（用户名来自 query，须防注入）
    u_esc = username.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    chips = "".join(
        '<a class="tg" href="/c/%s?tag=%s">#%s<span>%d</span></a>' % (
            u_enc, _up.quote(t), t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), c)
        for c, t in tags[:60]
    )
    html = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>标签</title>
<style>
body{margin:0;background:#0e1621;color:#e1e3e6;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;padding:16px}
h1{font-size:16px;margin:0 0 6px}.sub{color:#708499;font-size:12px;margin-bottom:14px}
.tgs{display:flex;flex-wrap:wrap;gap:8px}
.tg{display:inline-flex;align-items:center;gap:4px;background:#17212b;border:1px solid #1f2c38;border-radius:999px;padding:7px 13px;font-size:13px;color:#e1e3e6;text-decoration:none}
.tg:hover{border-color:#5eb5f7;color:#fff}
.tg span{background:#223248;border-radius:999px;padding:1px 8px;font-size:11px;color:#708499}
.back{display:inline-block;margin-bottom:14px;color:#5eb5f7;text-decoration:none;font-size:13px}
</style></head><body>
<a class="back" href="/c/%s">← 返回频道</a>
<h1># 全部标签</h1><div class="sub">点击标签只看该系列</div>
<div class="tgs">%s</div>
</body></html>""" % (u_esc, chips)
    return web.Response(status=200, content_type="text/html", text=html)


@routes.get("/more")
async def more_api(req: web.Request) -> web.Response:
    """加载更多：返回预渲染 HTML 片段（{html, next}），前端直接 insertAdjacentHTML，避免 JS 拼 HTML 转义地狱。"""
    if not _auth_ok(req):
        return web.Response(status=401, text="unauthorized")
    username = _channel_from_req(req)
    if not username:
        return web.json_response({"html": "", "next": 0})
    peer = await _get_peer(username)
    if not peer:
        return web.json_response({"html": "", "next": 0})
    try:
        offset_id = int(req.query.get("offset_id", "0") or 0)
    except ValueError:
        offset_id = 0
    tag = (req.query.get("tag", "") or "").strip()
    items, next_offset = await _fetch_page_with_groups(peer, username, offset_id, search=tag)
    frag = []
    for it in items:
        if isinstance(it, dict) and "group" in it:
            frag.append(_render_group(it))
        else:
            frag.append(_stream_item(it))
    return web.json_response({"html": "\n".join(frag), "next": next_offset})


def _item_json(m: Message, peer, username: str = "") -> dict:
    media = m.media
    # 纯文字消息无 file，get_file_name 会崩——text 消息不给 name
    name = get_file_name(m) if (m and m.file) else ""
    size = 0
    duration = 0
    doc = getattr(media, "document", None)
    if doc is not None:
        size = getattr(doc, "size", 0) or 0
        for attr in (getattr(doc, "attributes", None) or []):
            if isinstance(attr, (DocumentAttributeVideo, DocumentAttributeAudio)):
                duration = getattr(attr, "duration", 0) or 0
    elif getattr(media, "photo", None):
        sizes = getattr(media.photo, "sizes", []) or []
        if sizes:
            size = getattr(sizes[-1], "size", 0) or 0
    packed = _pack(peer.id if hasattr(peer, "id") else peer, m.id)
    tk = _thumb_kind(media)
    kind = "photo" if tk == "photo" else ("video" if doc is not None else "text")
    caption = (m.message or "").strip()
    q = ("?c=" + username) if username else ""
    return {
        "id": m.id,
        "packed": packed,
        "name": name,
        "size": size,
        "duration": duration,
        "kind": kind,
        "caption": caption[:400],
        "grouped": getattr(m, "grouped_id", None) or 0,
        "date": m.date.strftime("%Y-%m-%d"),
        "thumb": f"/thumb/{packed}{q}",
        "stream": f"/s/{packed}/{name}{q}",
        "watch": f"/watch/{packed}/{name}{q}",
    }


def _collect_items(msgs: list, peer, username: str = "") -> tuple:
    """把消息列表聚合成渲染项：同一 grouped_id 的连续消息合并为一个 album 组。
    返回 (items, next_offset)。next_offset = 最后一条参与渲染的消息 id。"""
    items = []
    next_offset = 0
    i = 0
    n = len(msgs)
    while i < n:
        m = msgs[i]
        if not _has_content(m):
            i += 1
            continue
        next_offset = m.id
        gid = getattr(m, "grouped_id", None) or 0
        if not gid:
            items.append(_item_json(m, peer, username))
            i += 1
            continue
        # 同组：向更旧方向收集同 grouped_id 的媒体消息
        group = [_item_json(m, peer, username)]
        j = i + 1
        while j < n:
            m2 = msgs[j]
            if (getattr(m2, "grouped_id", None) or 0) == gid and _has_content(m2):
                group.append(_item_json(m2, peer, username))
                next_offset = m2.id
                j += 1
            else:
                break
        # 组内按 id 升序（第一张 = 最新 id，与 TG 顺序一致：id 大 = 后发 = 在组内靠后位置）
        # TG 相册内 id 小的先发 → 排在左边/前面；这里保持消息列表顺序（id 降序）即第一张为 id 最大
        if len(group) > 1:
            items.append({"group": group, "grouped": gid, "chan": username})
        else:
            items.append(group[0])
        i = j
    return items, next_offset


async def _fetch_page_with_groups(peer, username: str, offset_id: int = 0, search: str = "") -> tuple:
    """取一页消息，若末尾组未闭合则补拉更旧消息直到组完整。返回 (items, next_offset)。
    search 非空时按标签/关键词服务器端搜索（get_messages(search=...)），分页同样带 search。
    Telegram 标签搜索需要 # 前缀，自动补。"""
    if search and not search.startswith("#"):
        search = "#" + search
    kw = dict(search=search) if search else {}
    msgs = await client.get_messages(entity=_msg_entity(peer), limit=PAGE_SIZE, offset_id=offset_id or 0, **kw)
    msgs = list(msgs)
    # 补拉：只要最后一条仍属于某组（且组内还有更旧成员未取到），继续向旧拉
    # 简化：取到最后一组完整为止（最多补 3 次）
    for _ in range(3):
        if not msgs:
            break
        last = msgs[-1]
        gid = getattr(last, "grouped_id", None) or 0
        if not gid or not _has_content(last):
            break
        # 检查组内是否还有更旧的同组成员未取到（组可能跨页）
        # 稳妥做法：直接多取一页，若新页第一条的同组 ID 与 last 相同则继续
        extra = await client.get_messages(entity=_msg_entity(peer), limit=PAGE_SIZE, offset_id=last.id, **kw)
        extra = list(extra)
        if not extra:
            break
        if (getattr(extra[0], "grouped_id", None) or 0) != gid:
            # 下一页第一条已经不是同组 → 组已完整
            break
        msgs.extend(extra)
    return _collect_items(msgs, peer, username)





def _fmt_size(n: int) -> str:
    if n >= 1 << 30: return f"{n / (1<<30):.1f} GB"
    if n >= 1 << 20: return f"{n / (1<<20):.1f} MB"
    if n >= 1 << 10: return f"{n / (1<<10):.0f} KB"
    return f"{n} B"


def _fmt_dur(sec: int) -> str:
    h, r = divmod(int(sec or 0), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@routes.get("/thumb/{id:\\d+}")
async def thumb(req: web.Request) -> web.Response:
    if not _auth_ok(req):
        return web.Response(status=401, text="unauthorized")
    packed = int(req.match_info["id"])
    msg_id = (packed >> msg_id_offset) & pack_bit_mask
    # 先校验频道再查缓存（缓存 key 含 c，避免跨频道串台 + 无 c 也能命中）
    c = req.query.get("c", "").strip()
    if not c:
        return web.Response(status=404, text="no channel")
    ckey = f"{msg_id}:{c}"
    if ckey in thumb_cache:
        data, mime = thumb_cache[ckey]
        return web.Response(body=data, content_type=mime,
                            headers={"Cache-Control": "public, max-age=86400"})
    peer = await _get_peer(c)
    if not peer:
        return web.Response(status=404, text="no peer")
    m = cast(Message, await client.get_messages(entity=_msg_entity(peer), ids=msg_id))
    if not m or not m.media or not _thumb_of(m.media):
        return web.Response(status=404, text="no thumb")
    try:
        data = await _load_thumb_bytes(m.media, packed)
    except Exception as e:
        log.warning(f"thumb download failed: {e}")
        return web.Response(status=404, text="no thumb")
    if not data:
        return web.Response(status=404, text="no thumb")
    if len(thumb_cache) >= THUMB_CACHE_MAX:
        thumb_cache.pop(next(iter(thumb_cache)))  # 超上限丢最早一条
    thumb_cache[ckey] = (data, "image/jpeg")
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@routes.get(r"/s/{id:\d+}/{name}")
async def channel_stream(req: web.Request) -> web.Response:
    """频道流式播放：用已解析的频道 peer 取消息（修复原版 InputPeerChannel access_hash=0 报错），支持 Range。"""
    if not _auth_ok(req):
        return web.Response(status=401, text="unauthorized")
    file_name = req.match_info["name"]
    packed = int(req.match_info["id"])
    msg_id = (packed >> msg_id_offset) & pack_bit_mask
    c = req.query.get("c", "").strip()
    if not c:
        return web.Response(status=404, text="404: Not Found")
    peer = await _get_peer(c)
    if not peer:
        return web.Response(status=404, text="404: Not Found")
    message = cast(Message, await client.get_messages(entity=_msg_entity(peer), ids=msg_id))
    if not message or not message.file or get_file_name(message) != file_name:
        return web.Response(status=404, text="404: Not Found")
    size = message.file.size
    is_download = req.query.get("download", "") == "1"
    try:
        offset = req.http_range.start or 0
        limit = req.http_range.stop or size
        if (limit > size) or (offset < 0) or (limit < offset):
            raise ValueError("range not in acceptable format")
    except ValueError:
        return web.Response(status=416, text="416: Range Not Satisfiable",
                            headers={"Content-Range": f"bytes */{size}"})
    if req.method != "HEAD":
        log.info(f"Streaming {message.id} Range: {offset}-{limit}")
        body = transfer.download(message.media, file_size=size, offset=offset, limit=limit)
    else:
        body = None
    # HTTP Content-Range end 为闭区间（end = start + len - 1），原版多算 1，微信 X5 严格校验会拒播
    end = limit - 1 if limit > 0 else 0
    headers = {
        "Content-Type": message.file.mime_type,
        "Content-Range": f"bytes {offset}-{end}/{size}",
        "Content-Length": str(limit - offset),
        "Accept-Ranges": "bytes",
    }
    # 微信 X5 内核：响应带 Content-Disposition（哪怕 inline）会被优先当文件下载而非视频流。
    # 默认完全不发该头（浏览器按 Content-Type 内联播放），仅显式 ?download=1 时用 attachment。
    if is_download:
        # 文件名来自 URL，需净化防 header 注入（引号/换行/CR）
        safe_name = file_name.replace('"', "").replace("\r", "").replace("\n", "").replace("\\", "")
        headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    if req.method == "HEAD":
        return web.Response(status=200, headers=headers)
    # 用 web.Response(body=async_gen) 方式（微信 X5 兼容，实测可播）。
    # 客户端断开时 aiohttp 会停止消费 generator；下载器内部对 GeneratorExit/CancelledError
    # 会取消所有在途分块，实现"停止播放即停止拉流"。
    return web.Response(status=206 if ("range" in req.headers or req.http_range.start is not None) else 200,
                        body=body, headers=headers)


@routes.post("/login")
async def login(req: web.Request) -> web.Response:
    """分享密码登录（访客）。登录后跳回来源页（next），默认 /c 频道总入口。"""
    data = await req.post()
    pw = data.get("password", "")
    next_url = (data.get("next") or "/c").strip()
    # 防开放重定向：仅允许站内相对路径（必须以单个 / 开头、不含反斜杠、非 // 协议相对）
    if not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url or "://" in next_url:
        next_url = "/c"
    if pw == ui_password:
        val = hashlib.sha256(("tgui:" + ui_password).encode()).hexdigest()
        resp = web.Response(status=302, headers={"Location": next_url})
        # 会话级 cookie（不设 max_age）：浏览器关闭即失效，退出后需重新输密码
        resp.set_cookie(AUTH_COOKIE, val, httponly=True, samesite="Lax")
        return resp
    return web.Response(status=200, content_type="text/html",
                        text=_login_html(next_url, err=True))


@routes.post("/admin/login")
async def admin_login(req: web.Request) -> web.Response:
    """管理密码登录。"""
    data = await req.post()
    pw = data.get("password", "")
    if pw == admin_password:
        val = hashlib.sha256(("tgadmin:" + admin_password).encode()).hexdigest()
        resp = web.Response(status=302, headers={"Location": "/manage"})
        # 会话级 cookie（不设 max_age）：浏览器关闭即失效，退出后需重新输密码
        resp.set_cookie(ADMIN_COOKIE, val, httponly=True, samesite="Lax")
        return resp
    return web.Response(status=200, content_type="text/html",
                        text=ADMIN_LOGIN_HTML.replace("</form>", "<p style='color:#f87171;margin-top:12px'>密码错误</p></form>"))


@routes.get(r"/watch/{id:\d+}/{name}")
async def watch_page(req: web.Request) -> web.Response:
    if not _auth_ok(req):
        return web.Response(status=200, content_type="text/html", text=_login_html(req.path_qs))
    packed = int(req.match_info["id"])
    name = req.match_info["name"]
    c = req.query.get("c", "").strip()
    if not c:
        return web.Response(status=404, text="404: Not Found")
    peer = await _get_peer(c)
    if not peer:
        return web.Response(status=404, text="404: Not Found")
    import urllib.parse as _up
    c_q = _up.quote(c)      # URL 编码（防属性注入；_get_peer 已挡非法名，双保险）
    msg_id = (packed >> msg_id_offset) & pack_bit_mask
    m = cast(Message, await client.get_messages(entity=_msg_entity(peer), ids=msg_id))
    if not m or not m.media:
        return web.Response(status=404, text="404: Not Found")
    size = getattr(getattr(m.media, "document", None) or m.media, "size", 0)
    q = "?c=" + c_q
    stream = f"/s/{packed}/{name}{q}"
    thumb_url = f"/thumb/{packed}{q}"
    is_photo = _thumb_kind(m.media) == "photo"
    if is_photo:
        player = '<img src="%s" style="width:100%%;border-radius:12px;background:#000" alt="图片">' % thumb_url
    else:
        # 微信 X5 内核：playsinline + x5-video-player-type=h5 让视频在页面内播放（而非全屏/下载）
        player = ('<video controls preload="metadata" playsinline webkit-playsinline '
                  'x5-playsinline x5-video-player-type="h5" src="%s"></video>') % stream
    dl_hint = ""  # 用户要求不提供下载按钮
    name_esc = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    back_href = "/c/" + c_q  # 返回频道页（URL 已编码，根路径是管理入口不能回 /）
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name_esc}</title>
<style>
body{{margin:0;background:#0b0f17;color:#e5e7eb;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}}
.top{{padding:16px 24px;display:flex;align-items:center;gap:12px;background:#111827;border-bottom:1px solid #1f2937}}
.top a{{color:#93c5fd;text-decoration:none;font-size:14px}}
.vwrap{{max-width:1100px;margin:24px auto;padding:0 16px}}
video{{width:100%;border-radius:12px;background:#000;box-shadow:0 8px 30px rgba(0,0,0,.5)}}
.meta{{margin-top:14px;font-size:14px;color:#9ca3af;word-break:break-all}}
.dl{{display:inline-block;margin-top:16px;padding:10px 22px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;font-size:14px}}
.dl:hover{{background:#1d4ed8}}
</style></head><body>
<div class="top"><a href="{back_href}">← 返回列表</a></div>
<div class="vwrap">{player}
<div class="meta">{name_esc}<br>大小 {_fmt_size(size)}</div>
{dl_hint}</div></body></html>"""
    return web.Response(status=200, content_type="text/html", text=html)


def render_list_html(msgs_html: str, next_offset: int, peer, chan: dict = None, username: str = "", show_all: bool = False, tag: str = "") -> str:
    """渲染频道消息页。
    show_all=True：显示频道切换条（分享 /c 时，所有频道可切换）；
    show_all=False：锁定当前频道（分享 /c/<频道> 时，仅浏览单频道，无切换条、无管理入口）。
    tag 非空：标签筛选态，标签栏高亮当前标签。
    """
    chan = chan or {"name": "频道", "subs": "", "avatar": "/avatar"}
    msgs = msgs_html
    first_next = str(next_offset or 0)
    display = "block" if next_offset else "none"
    switch = ""
    if show_all and len(_channel_list) > 1:
        switch = "".join(
            ('<a class="sw %s" href="/c/%s">%s</a>' % (
                "on" if c.get("username") == username else "",
                c.get("username", ""),
                (c.get("title") or c.get("username") or "").replace("<", "&lt;").replace(">", "&gt;"),
            ))
            for c in _channel_list
        )
    # 频道名链接：show_all 时点击回选择页；锁定时不可跳转
    if show_all:
        name_link = '<a href="/" style="color:inherit;text-decoration:none">%s</a>' % (chan.get("name") or "频道").replace("<", "&lt;").replace(">", "&gt;")
        avatar_link = '<a class="avatar" href="/" title="频道列表">'
    else:
        name_link = (chan.get("name") or "频道").replace("<", "&lt;").replace(">", "&gt;")
        avatar_link = '<div class="avatar">'
    html = LIST_HTML_TEMPLATE
    html = (html.replace("__MSGS__", msgs).replace("__FIRST_NEXT__", first_next)
                .replace("__DISPLAY__", display)
                .replace("__CHAN_NAME__", name_link)
                .replace("__CHAN_SUBS__", chan.get("subs") or "")
                .replace("__CHAN_AVATAR_OPEN__", avatar_link)
                .replace("__CHAN_AVATAR_CLOSE__", "</div>" if not show_all else "</a>")
                .replace("__CHAN_AVATAR__", (chan.get("avatar") or "/avatar").replace("&", "&amp;"))
                .replace("__CHAN_SWITCH__", switch)
                .replace("__CHAN__", username.replace("\\", "\\\\").replace('"', '\\"'))
                .replace("__DISPLAY_SWITCH__", "flex" if (show_all and len(_channel_list) > 1) else "none")
                .replace("__CHAN_URL__", "/c/%s" % (username or "?"))
                .replace("__CURTAG__", tag.replace("\\", "\\\\").replace('"', '\\"')))
    return html


LIST_HTML_TEMPLATE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>频道消息</title>
<style>
/* Telegram Web 深色风格 */
:root{--bg:#0e1621;--panel:#17212b;--card:#182533;--border:#1f2c38;--txt:#e1e3e6;--sub:#708499;--acc:#5eb5f7;--accent:#64b5ef}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,'PingFang SC','Microsoft YaHei',Roboto,sans-serif;font-size:14px}
/* 顶部：频道信息（TG 风格居中） */
.header{position:sticky;top:0;z-index:30;background:rgba(14,22,33,.96);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:10px 16px;display:flex;align-items:center;gap:10px}
.chan-head{display:flex;align-items:center;gap:10px;flex:1;min-width:0}
.avatar{width:40px;height:40px;border-radius:50%;overflow:hidden;flex-shrink:0;background:linear-gradient(135deg,#3b82f6,#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:19px}
.avatar img{width:100%;height:100%;object-fit:cover}
.chan-info{min-width:0;line-height:1.25}
.chan-info h1{margin:0;font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chan-info .sub{font-size:12px;color:var(--sub)}
/* 消息列表：TG 网页版居中窄列 */
.feed{max-width:728px;margin:0 auto;padding:12px 10px 24px}
/* 日期分隔（TG 风格小胶囊） */
.daysep{text-align:center;margin:14px 0 8px}
.daysep span{background:var(--panel);color:var(--sub);font-size:12px;padding:4px 12px;border-radius:999px}
/* 消息卡片：TG 气泡 */
.msg{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;cursor:pointer;overflow:hidden;transition:border-color .12s}
.msg:hover{border-color:#314a63}
.msg .media{position:relative;background:#0c1219}
.msg .media img{width:100%;display:block;background:#0c1219}
.msg .media.video img{aspect-ratio:16/9;object-fit:cover}
.msg .media.photo img{aspect-ratio:auto;max-height:62vh;object-fit:contain;margin:0 auto}
.msg .media .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:44px;background:rgba(14,22,33,.5)}
.msg .media .dur{position:absolute;right:8px;bottom:8px;background:rgba(0,0,0,.82);color:#fff;font-size:11px;padding:3px 8px;border-radius:7px;letter-spacing:.3px}
/* TG 圆形播放按钮 */
.msg .media .btn-play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:56px;height:56px;border-radius:50%;background:rgba(0,0,0,.5);border:2px solid rgba(255,255,255,.92);display:flex;align-items:center;justify-content:center;pointer-events:none;backdrop-filter:blur(2px)}
.msg .media .btn-play::before{content:'';border-left:20px solid #fff;border-top:12px solid transparent;border-bottom:12px solid transparent;margin-left:4px}
/* 文字区 */
.msg .body{padding:10px 12px 12px}
.msg .cap{font-size:14px;line-height:1.55;color:var(--txt);word-break:break-word;white-space:pre-wrap;margin:0}
.msg .cap a.hash{color:var(--acc);text-decoration:none;font-weight:500}
.msg .cap a.hash:active{opacity:.7}
/* 图片 lightbox 查看器 */
#lb{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.94);display:none;flex-direction:column;align-items:center;justify-content:center;touch-action:none;user-select:none;-webkit-user-select:none}
#lb.open{display:flex}
#lb img.lb-img{max-width:96vw;max-height:82vh;object-fit:contain;border-radius:6px;background:#000;box-shadow:0 8px 40px rgba(0,0,0,.6)}
#lb .lb-top{position:absolute;top:0;left:0;right:0;display:flex;justify-content:space-between;align-items:center;padding:14px 18px;color:#fff;font-size:13px;z-index:2}
#lb .lb-x{width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.12);border:none;color:#fff;font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center}
#lb .lb-x:active{background:rgba(255,255,255,.25)}
#lb .lb-nav{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:44px;border-radius:50%;background:rgba(255,255,255,.12);border:none;color:#fff;font-size:22px;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:2}
#lb .lb-nav:active{background:rgba(255,255,255,.25)}
#lb .lb-prev{left:12px}
#lb .lb-next{right:12px}
#lb .lb-cap{position:absolute;bottom:0;left:0;right:0;padding:14px 18px 22px;color:#cfd6e0;font-size:13px;line-height:1.5;background:linear-gradient(transparent,rgba(0,0,0,.75));max-height:22vh;overflow-y:auto;white-space:pre-wrap;word-break:break-word}
#lb .lb-idx{color:#9fb0c3;margin-right:12px}
@media(max-width:600px){#lb .lb-nav{width:36px;height:36px;font-size:18px}#lb img.lb-img{max-height:78vh}}
.msg .meta{display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:12px;color:var(--sub)}
.msg .meta .date{color:var(--sub)}
/* 加载提示 */
.loading{text-align:center;color:var(--sub);font-size:13px;padding:16px 0 26px}
.refresh-btn{background:transparent;border:1px solid var(--border);color:var(--sub);border-radius:8px;padding:5px 12px;font-size:12px;cursor:pointer}
.refresh-btn:hover{color:var(--txt);border-color:#314a63}
.btn-mini{background:transparent;border:1px solid var(--border);color:var(--sub);border-radius:8px;padding:5px 9px;font-size:13px;cursor:pointer;text-decoration:none;line-height:1.4}
/* 标签栏（横向滚动 chips） */
.tagbar{position:sticky;top:61px;z-index:20;display:flex;gap:8px;overflow-x:auto;padding:8px 16px;background:rgba(14,22,33,.95);backdrop-filter:blur(8px);border-bottom:1px solid var(--border);scrollbar-width:none}
.tagbar::-webkit-scrollbar{display:none}
.tag{flex-shrink:0;font-size:12px;color:var(--sub);background:var(--panel);border:1px solid var(--border);border-radius:999px;padding:5px 13px;text-decoration:none;white-space:nowrap}
.tag:hover{color:var(--txt)}
.tag.on{color:#0e1621;background:var(--acc);border-color:var(--acc);font-weight:600}
.tag .cnt{opacity:.7;margin-left:3px;font-size:11px}
.tag-clear{color:#f87171;border-color:#5b1e1e}
.btn-mini:hover{color:var(--txt);border-color:#314a63}
.ops{display:flex;align-items:center;gap:8px;margin-left:auto}
.chanswitch{position:sticky;top:61px;z-index:20;display:flex;gap:8px;overflow-x:auto;padding:8px 16px;background:rgba(14,22,33,.95);backdrop-filter:blur(8px);border-bottom:1px solid var(--border);scrollbar-width:none}
.chanswitch::-webkit-scrollbar{display:none}
.sw{flex-shrink:0;font-size:12px;color:var(--sub);background:var(--panel);border:1px solid var(--border);border-radius:999px;padding:5px 13px;text-decoration:none;white-space:nowrap}
.sw:hover{color:var(--txt)}
.sw.on{color:#0e1621;background:var(--acc);border-color:var(--acc);font-weight:600}
@media(min-width:600px){.feed{padding:14px 14px 28px}}
.msg.album{cursor:default}
.agrid{display:grid;gap:3px;background:var(--card)}
.agrid a{position:relative;display:block;overflow:hidden;background:#0c1219;min-height:60px}
.agrid img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .15s}
.agrid a:active img{transform:scale(1.03)}
.agrid a .btn-play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:44px;height:44px;border-radius:50%;background:rgba(0,0,0,.55);border:2px solid rgba(255,255,255,.9);display:flex;align-items:center;justify-content:center;pointer-events:none}
.agrid a .btn-play::before{content:'';border-left:16px solid #fff;border-top:9px solid transparent;border-bottom:9px solid transparent;margin-left:3px}
.agrid a .dur{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.72);color:#fff;font-size:11px;padding:2px 7px;border-radius:6px;pointer-events:none}
/* 2 张：左右各半 */
.agrid.n2{grid-template-columns:1fr 1fr;grid-auto-rows:180px}
/* 3 张：首图占左大，右列上下两格 */
.agrid.n3{grid-template-columns:2fr 1fr;grid-auto-rows:150px}
.agrid.n3 a:first-child{grid-row:span 2}
/* 4 张：2x2 */
.agrid.n4{grid-template-columns:1fr 1fr;grid-auto-rows:170px}
/* 5+ 张：首图整行 + 其余 3 列 */
.agrid.n5,.agrid.n6,.agrid.n7,.agrid.n8,.agrid.n9,.agrid.n10{grid-template-columns:1fr 1fr 1fr;grid-auto-rows:130px}
.agrid.n5 a:first-child,.agrid.n6 a:first-child,.agrid.n7 a:first-child,.agrid.n8 a:first-child,.agrid.n9 a:first-child,.agrid.n10 a:first-child{grid-column:span 3;grid-row:span 2}
@media(max-width:480px){.agrid.n2{grid-auto-rows:140px}.agrid.n3{grid-auto-rows:120px}.agrid.n4{grid-auto-rows:130px}.agrid.n5,.agrid.n6,.agrid.n7,.agrid.n8,.agrid.n9,.agrid.n10{grid-auto-rows:110px}}
</style></head><body>
<div class="header">
<div class="chan-head">
  __CHAN_AVATAR_OPEN__<img src="__CHAN_AVATAR__" onerror="this.style.display='none';this.parentElement.textContent='📺'">__CHAN_AVATAR_CLOSE__
  <div class="chan-info"><h1>__CHAN_NAME__</h1><span class="sub">__CHAN_SUBS__ · <span id="count"></span></span></div>
</div>
<button class="refresh-btn" onclick="location.reload()">刷新</button>
</div>
<div class="chanswitch" id="chanswitch" style="display:__DISPLAY_SWITCH__">__CHAN_SWITCH__</div>
<div class="tagbar" id="tagbar" style="display:none"></div>
<div class="feed" id="feed">__MSGS__</div>
<div class="loading" id="sentinel" data-next="__FIRST_NEXT__" style="display:__DISPLAY__">↑ 上滑加载更多 ↑</div>
<div id="lb"><div class="lb-top"><span class="lb-idx" id="lbIdx"></span><button class="lb-x" onclick="lbClose()">✕</button></div><button class="lb-nav lb-prev" onclick="lbPrev()">‹</button><img class="lb-img" id="lbImg" alt=""><button class="lb-nav lb-next" onclick="lbNext()">›</button><div class="lb-cap" id="lbCap"></div></div>
<script>
const CHAN="__CHAN__";
const CURTAG="__CURTAG__";
const feed=document.getElementById('feed'), sentinel=document.getElementById('sentinel');
let loading=false;
function updCount(){
  const c=document.getElementById('count');
  if(c) c.textContent=feed.querySelectorAll('.msg').length+' 条消息';
}
updCount();
// —— 图片 Lightbox：点击图片全屏浏览，左右滑动/箭头/键盘切换上一张下一张 ——
let lbPhotos=[], lbIdx=0, lbX=0;
function lbCollect(){
  lbPhotos=[];
  // 单条图片消息
  feed.querySelectorAll('.msg[data-watch] .media.photo img').forEach(img=>{
    const m=img.closest('.msg');
    const cap=m.querySelector('.cap')?m.querySelector('.cap').textContent.trim():'';
    lbPhotos.push({src:img.src, watch:m.dataset.watch, cap});
  });
  // 相册组里的图片格
  feed.querySelectorAll('.msg.album a.at img[alt="图片"]').forEach(img=>{
    const a=img.closest('a.at');
    const cap=a.closest('.msg.album')?((a.closest('.msg.album').querySelector('.cap')||{}).textContent||'').trim():'';
    lbPhotos.push({src:img.src, watch:a.href, cap});
  });
}
function lbShow(){
  const p=lbPhotos[lbIdx]; if(!p) return;
  const im=document.getElementById('lbImg'); im.src=p.src; im.alt='图片';
  const cap=document.getElementById('lbCap'); cap.textContent=p.cap||'';
  document.getElementById('lbIdx').textContent=(lbIdx+1)+' / '+lbPhotos.length;
  document.getElementById('lb').classList.add('open');
}
function lbOpenFrom(imgEl){
  lbCollect();
  // 找该图在收集列表中的位置（src 相同）
  lbIdx=lbPhotos.findIndex(p=>p.src===imgEl.src); if(lbIdx<0) lbIdx=0;
  lbShow();
}
function lbClose(){ document.getElementById('lb').classList.remove('open'); }
function lbPrev(){ if(lbPhotos.length<2) return; lbIdx=(lbIdx-1+lbPhotos.length)%lbPhotos.length; lbShow(); }
function lbNext(){ if(lbPhotos.length<2) return; lbIdx=(lbIdx+1)%lbPhotos.length; lbShow(); }
// 键盘左右
document.addEventListener('keydown',e=>{
  if(!document.getElementById('lb').classList.contains('open')) return;
  if(e.key==='ArrowLeft') lbPrev(); else if(e.key==='ArrowRight') lbNext(); else if(e.key==='Escape') lbClose();
});
// 触摸左右滑动
const lbEl=document.getElementById('lb');
lbEl.addEventListener('touchstart',e=>{ lbX=e.touches[0].clientX; },{passive:true});
lbEl.addEventListener('touchend',e=>{
  const dx=e.changedTouches[0].clientX-lbX;
  if(Math.abs(dx)>48){ dx<0?lbNext():lbPrev(); }
},{passive:true});
feed.addEventListener('click',e=>{
  // 点击图片 → 打开 lightbox（单条 photo 或相册组里的图片格）
  const pimg=e.target.closest('.media.photo img, .msg.album a.at img[alt="图片"]');
  if(pimg){ e.preventDefault(); lbOpenFrom(pimg); return; }
  // 点击 caption 内的 #标签 链接：走默认 a 跳转，不拦截为 watch
  const a=e.target.closest('a'); if(a) return;
  const m=e.target.closest('.msg'); if(m&&m.dataset.watch) location.href=m.dataset.watch;
});
async function loadMore(){
  if(loading) return; loading=true;
  const off=sentinel.dataset.next; if(!off){loading=false;return}
  sentinel.textContent='加载中…';
  try{
    const tg=CURTAG?('&tag='+encodeURIComponent(CURTAG)):'';
    const r=await fetch('/more?offset_id='+off+'&c='+encodeURIComponent(CHAN)+tg); const d=await r.json();
    if(d.next===0){sentinel.textContent='— 到底了 —';sentinel.dataset.next='';loading=false;return}
    feed.insertAdjacentHTML('beforeend',d.html);
    sentinel.dataset.next=d.next; sentinel.textContent='↑ 上滑加载更多 ↑';
    updCount();
  }catch(e){sentinel.textContent='加载失败，继续下滑重试';}
  loading=false;
}
// 标签栏：拉取频道高频标签，渲染 chips；点击进入标签筛选视图
async function loadTags(){
  const bar=document.getElementById('tagbar'); if(!bar) return;
  try{
    const r=await fetch('/tags?c='+encodeURIComponent(CHAN)); const d=await r.json();
    if(!d.tags || !d.tags.length){return;}
    let html='';
    if(CURTAG){html+='<a class="tag tag-clear" href="/c/'+encodeURIComponent(CHAN)+'">✕ 全部</a>';}
    for(const t of d.tags){
      const on=(CURTAG===t.tag)?' on':'';
      const ts=String(t.tag).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
      html+='<a class="tag'+on+'" href="/c/'+encodeURIComponent(CHAN)+'?tag='+encodeURIComponent(t.tag)+'">#'+ts+'<span class="cnt">'+t.count+'</span></a>';
    }
    html+='<a class="tag" href="/cloud?c='+encodeURIComponent(CHAN)+'">🔍 全部标签</a>';
    bar.innerHTML=html; bar.style.display='flex';
  }catch(e){/* 标签加载失败静默，不影响主流程 */}
}
function nearBottom(){ return sentinel.getBoundingClientRect().top < window.innerHeight + 300; }
let ticking=false;
window.addEventListener('scroll',()=>{
  if(ticking) return; ticking=true;
  requestAnimationFrame(()=>{ ticking=false; if(nearBottom()) loadMore(); });
});
function checkLoad(){ if(nearBottom()) loadMore(); }
if('IntersectionObserver' in window){
  const io=new IntersectionObserver(es=>{es.forEach(e=>{if(e.isIntersecting)loadMore();});},{rootMargin:'400px'});
  io.observe(sentinel);
}
const iv=setInterval(checkLoad, 1200);
window.addEventListener('load',checkLoad);
loadTags();
</script></body></html>"""






def _linkify_tags(caption_html: str, chan: str) -> str:
    """把 caption HTML 中的 #标签 转为可点击链接（跳标签筛选视图）。
    输入已是 HTML 转义后的字符串，正则匹配 #中文/英文标签。"""
    import re as _re
    import urllib.parse as _up
    if not chan:
        return caption_html
    chan_enc = _up.quote(chan)
    def _rep(m: _re.Match) -> str:
        t = m.group(1)
        href = "/c/%s?tag=%s" % (chan_enc, _up.quote(t))
        return '<a class="hash" href="%s">#%s</a>' % (href, t)
    # 标签规则：#后跟中文/字母/数字/·-，到空白或结尾；# 前不得是 &（避免破坏 &#35; 等 HTML 实体）
    return _re.sub(r"(?<![&])#([\w\u4e00-\u9fff][\w\u4e00-\u9fff·\-]*)", _rep, caption_html)


def _stream_item(it: dict) -> str:
    """TG 消息模式卡片：一条消息 = 媒体（图/视频缩略图）+ 文字 caption + 日期大小。"""
    dur = _fmt_dur(it["duration"])
    dur_badge = '<span class="dur">%s</span>' % dur if dur else ""
    name = it["name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    caption = it.get("caption") or ""
    caption = caption.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    # #标签 → 可点击链接（跳标签筛选），频道名从 watch URL 提取
    import urllib.parse as _up
    _chan = _up.parse_qs(_up.urlsplit(it.get("watch", "")).query).get("c", [""])[0]
    caption = _linkify_tags(caption, _chan)
    watch = it["watch"]
    kind = it.get("kind", "video")
    if kind == "text":
        return ('<div class="msg">'
                '<div class="body"><div class="cap">%s</div>'
                '<div class="meta"><span></span><span class="date">%s</span></div></div></div>') % (
                    caption or it["date"], it["date"])

    # 消息主体：视频→缩略图+时长；图片→直接显示大图；纯文字→无媒体
    if kind == "photo":
        media_html = ('<div class="media photo">'
                      '<img loading="lazy" src="%s" alt="图片" '
                      'onerror="this.style.display=\'none\';this.parentElement.classList.add(\'nophoto\')">'
                      '</div>') % it["thumb"]
    elif kind == "video":
        media_html = ('<div class="media video">'
                      '<img loading="lazy" src="%s" alt="视频" '
                      'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'none\';this.nextElementSibling.nextElementSibling.style.display=\'flex\'">'
                      '<div class="btn-play"></div>'
                      '<div class="ph" style="display:none">▶</div>%s</div>') % (it["thumb"], dur_badge)
    else:
        media_html = ""

    # 用户要求不显示文件名：媒体消息只展示 caption（如有）+ 日期/大小
    if caption:
        cap_html = '<div class="cap">%s</div>' % caption
    else:
        cap_html = ""
    meta = '<div class="meta"><span>%s</span><span class="date">%s</span></div>' % (_fmt_size(it["size"]), it["date"])
    return ('<div class="msg" data-watch="%s">'
            '<div class="body">%s%s%s</div></div>') % (
        watch, media_html, cap_html, meta)


def _render_group(grp: dict) -> str:
    """TG 相册（album）渲染：多条同组媒体合成一张卡片，首图大 + 其余网格排列。"""
    group = grp["group"]
    items = []
    caption = ""
    total_size = 0
    # 组内消息顺序：TG 相册先发的在前（id 小的在前）→ 显示顺序保持列表里的 id 升序？
    # _collect_items 里 group 是按 id 降序收集的（m 是最新 id），需翻转成 id 升序（先发在前）
    group_sorted = sorted(group, key=lambda x: x["id"])
    for it in group_sorted:
        total_size += it.get("size", 0)
        if not caption and it.get("caption"):
            caption = it["caption"]
        dur = _fmt_dur(it["duration"])
        dur_badge = '<span class="dur">%s</span>' % dur if dur else ""
        kind = it.get("kind", "video")
        if kind == "photo":
            tile = ('<a class="at" href="%s" title="查看原图">'
                    '<img loading="lazy" src="%s" alt="图片" '
                    "onerror=\"this.parentElement.style.display='none'\">"
                    '</a>') % (it["watch"], it["thumb"])
        elif kind == "video":
            tile = ('<a class="at" href="%s" title="播放视频">'
                    '<img loading="lazy" src="%s" alt="视频" '
                    "onerror=\"this.style.display='none';this.nextElementSibling.style.display='flex'\">"
                    '<div class="btn-play"></div>%s</a>') % (it["watch"], it["thumb"], dur_badge)
        else:
            tile = ""
        items.append(tile)
    cap_html = ('<div class="cap">%s</div>' % _linkify_tags(
        caption.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>"),
        grp.get("chan", ""))) if caption else ""
    meta = '<div class="meta"><span>%s · %d 张</span><span class="date">%s</span></div>' % (_fmt_size(total_size), len(group_sorted), group_sorted[-1]["date"])
    return ('<div class="msg album">'
            '<div class="agrid n%d">%s</div>%s%s</div>') % (
        len(group_sorted), "".join(items), cap_html, meta)


LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>访问验证</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0f17;color:#e5e7eb;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}
.box{background:#151b28;border:1px solid #1f2937;border-radius:16px;padding:40px;width:340px;text-align:center}
h1{font-size:20px;margin:0 0 8px;background:linear-gradient(90deg,#60a5fa,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
p{color:#9ca3af;font-size:14px;margin:0 0 24px}
input{width:100%;padding:12px;border:1px solid #334155;border-radius:10px;background:#0f172a;color:#fff;font-size:16px;outline:none}
input:focus{border-color:#3b82f6}
button{margin-top:16px;width:100%;padding:12px;background:#2563eb;border:none;border-radius:10px;color:#fff;font-size:16px;cursor:pointer}
button:hover{background:#1d4ed8}
</style></head><body>
<div class="box"><h1>🔒 访问验证</h1><p>请输入访问密码</p>
<form method="post" action="/login"><input type="hidden" name="next" value="__NEXT__"><input type="password" name="password" placeholder="密码" autofocus><button type="submit">进入</button></form>
</div></body></html>"""


def _login_html(next_url: str = "/c", err: bool = False) -> str:
    """渲染登录页，回显回跳地址（next）并在错误时提示。"""
    import html as _h
    nxt = _h.escape(next_url)
    err_html = "<p style='color:#f87171;margin-top:12px'>密码错误</p>" if err else ""
    return LOGIN_HTML.replace("__NEXT__", nxt).replace("</form>", err_html + "</form>")


ADMIN_LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>管理登录</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0e1621;color:#e1e3e6;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}
.box{background:#17212b;border:1px solid #1f2c38;border-radius:16px;padding:40px;width:340px;text-align:center}
h1{font-size:20px;margin:0 0 8px;background:linear-gradient(90deg,#f59e0b,#ef4444);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
p{color:#708499;font-size:14px;margin:0 0 24px}
input{width:100%;padding:12px;border:1px solid #2a3a49;border-radius:10px;background:#0e1621;color:#e1e3e6;font-size:16px;outline:none}
input:focus{border-color:#5eb5f7}
button{margin-top:16px;width:100%;padding:12px;background:#d97706;border:none;border-radius:10px;color:#fff;font-size:16px;cursor:pointer}
button:hover{background:#b45309}
</style></head><body>
<div class="box"><h1>⚙️ 管理登录</h1><p>请输入管理密码</p>
<form method="post" action="/admin/login"><input type="password" name="password" placeholder="管理密码" autofocus><button type="submit">进入管理</button></form>
</div></body></html>"""


CHANNEL_PICKER_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>选择频道</title>
<style>
body{margin:0;min-height:100vh;background:#0e1621;color:#e1e3e6;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;padding:24px 16px}
h1{font-size:18px;margin:0 0 6px}
.sub{color:#708499;font-size:13px;margin-bottom:18px}
.pick{display:flex;align-items:center;gap:12px;background:#17212b;border:1px solid #1f2c38;border-radius:14px;padding:12px 14px;margin-bottom:10px;cursor:pointer;text-decoration:none;color:inherit;transition:border-color .12s}
.pick:hover{border-color:#314a63}
.p-avatar{width:44px;height:44px;border-radius:50%;overflow:hidden;flex-shrink:0;background:linear-gradient(135deg,#3b82f6,#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:20px}
.p-avatar img{width:100%;height:100%;object-fit:cover}
.p-info{flex:1;min-width:0}
.p-name{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.p-user{font-size:12px;color:#708499}
.p-tag{font-size:11px;color:#5eb5f7;background:rgba(94,181,247,.12);padding:3px 9px;border-radius:999px;flex-shrink:0}
.manage{display:inline-block;margin-top:16px;color:#5eb5f7;font-size:13px;text-decoration:none}
</style></head><body>
<h1>📺 频道代理</h1>
<div class="sub">选择一个频道进入</div>
__PICKER_ROWS__
</body></html>"""


MANAGE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>频道管理</title>
<style>
body{margin:0;min-height:100vh;background:#0e1621;color:#e1e3e6;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;padding:24px 16px;max-width:760px;margin:0 auto}
h1{font-size:18px;margin:0 0 4px}
.sub{color:#708499;font-size:13px;margin-bottom:20px}
.back{color:#5eb5f7;font-size:13px;text-decoration:none;display:inline-block;margin-bottom:16px}
.addbox{background:#17212b;border:1px solid #1f2c38;border-radius:14px;padding:14px;margin-bottom:18px}
.addbox input{flex:1;background:#0e1621;border:1px solid #2a3a49;border-radius:8px;color:#e1e3e6;padding:10px 12px;font-size:14px;outline:none;width:100%;box-sizing:border-box;margin-bottom:10px}
.addbox input:focus{border-color:#5eb5f7}
.btn{background:#2563eb;border:none;border-radius:8px;color:#fff;font-size:13px;padding:9px 16px;cursor:pointer}
.btn:hover{background:#1d4ed8}
.btn.small{padding:5px 10px;font-size:12px;border-radius:6px;background:#2a3a49;color:#e1e3e6}
.btn.small:hover{background:#36475c}
.btn.danger{background:transparent;border:1px solid #5f3a3a;color:#f87171}
.btn.danger:hover{background:rgba(248,113,113,.08)}
.row{display:flex;align-items:center;gap:12px;background:#17212b;border:1px solid #1f2c38;border-radius:12px;padding:12px 14px;margin-bottom:8px}
.row.active{border-color:#2b4a6b}
.row .info{flex:1;min-width:0}
.row .t{font-size:14px;font-weight:600}
.row .u{font-size:12px;color:#708499}
.row .ops{display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap}
.tag{font-size:11px;color:#5eb5f7;background:rgba(94,181,247,.12);padding:2px 8px;border-radius:999px}
.add-title{font-size:14px;font-weight:600;margin-bottom:10px}
.my-title{font-size:13px;color:#5eb5f7;cursor:pointer;margin-top:10px;padding:4px 0}
#mineWrap{margin-top:8px}
#mineList{margin-top:10px;max-height:360px;overflow-y:auto;border:1px solid #1f2c38;border-radius:10px;background:#0e1621}
.mine-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid #1f2c38;cursor:pointer}
.mine-item:hover{background:#1a2634}
.mine-item:last-child{border-bottom:none}
.mine-item .t{flex:1;min-width:0;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mine-item .u{font-size:12px;color:#708499;flex-shrink:0}
.mine-item .k{font-size:11px;color:#5eb5f7;background:rgba(94,181,247,.12);padding:2px 8px;border-radius:999px;flex-shrink:0}
.mine-item .add2{font-size:12px;color:#86efac;flex-shrink:0}
.pwbox{background:#17212b;border:1px solid #1f2c38;border-radius:14px;padding:14px;margin-bottom:18px}
.pwrow{display:flex;gap:8px;flex-wrap:wrap}
.pwrow select,.pwrow input{flex:1;min-width:120px;background:#0e1621;border:1px solid #2a3a49;border-radius:8px;color:#e1e3e6;padding:9px 10px;font-size:13px;outline:none}
.pwrow select:focus,.pwrow input:focus{border-color:#5eb5f7}
#msg{color:#fca5a5;font-size:13px;margin-top:10px;min-height:18px}
</style></head><body>
<a class="back" href="/">← 返回</a>&nbsp;&nbsp;<a class="back" href="/upload" style="color:#86efac">📤 上传中心</a>
<h1>⚙️ 频道管理</h1>
<div class="sub">管理代理的频道：添加、删除、复制分享链接、改密码</div>
<div class="addbox">
  <div class="add-title">添加频道</div>
  <input type="text" id="newChan" placeholder="手动输入：t.me/xxx 或 @xxx 或 频道ID（如 -100123456789）">
  <button class="btn" onclick="addChan()">添加</button>
  <div class="my-title" onclick="toggleMine()">▼ 从我的账号加载频道</div>
  <div id="mineWrap" style="display:none">
    <button class="btn" onclick="loadMine()">📂 加载我的频道</button>
    <div id="mineList"></div>
  </div>
  <div id="msg"></div>
</div>
<div class="pwbox">
  <div class="add-title">修改密码</div>
  <div class="pwrow">
    <select id="pwKind"><option value="share">分享密码（访客）</option><option value="admin">管理密码</option></select>
    <input type="password" id="pwVal" placeholder="新密码（至少4位）">
    <button class="btn" onclick="changePw()">修改</button>
  </div>
</div>
<div id="rows">__CHANNEL_ROWS__</div>
<script>
function setMsg(m, ok){const el=document.getElementById('msg');el.textContent=m;el.style.color=ok?'#86efac':'#fca5a5';}
function copyShare(url){
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(url).then(()=>setMsg('已复制: '+url,true)).catch(()=>fallbackCopy(url));
  } else fallbackCopy(url);
}
function fallbackCopy(url){
  const ta=document.createElement('textarea'); ta.value=url; document.body.appendChild(ta);
  ta.select(); try{document.execCommand('copy'); setMsg('已复制: '+url,true);}catch(e){setMsg('复制失败，手动复制: '+url,false);}
  document.body.removeChild(ta);
}
function toggleMine(){const w=document.getElementById('mineWrap');w.style.display=w.style.display==='none'?'block':'none';}
async function loadMine(){
  const list=document.getElementById('mineList');
  list.innerHTML='<div style="padding:12px;color:#708499">加载中…</div>';
  const r=await fetch('/manage/api/dialogs'); const d=await r.json();
  if(!d.ok){list.innerHTML='<div style="padding:12px;color:#f87171">'+d.msg+'</div>';return;}
  if(!d.items.length){list.innerHTML='<div style="padding:12px;color:#708499">没有可用的频道</div>';return;}
  const added=new Set(__ADDED_SET__);
  list.innerHTML=d.items.map(i=>{
    const u=i.username||('id:'+i.id);
    const nm=String(i.name).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    return '<div class="mine-item" data-u="'+u+'">'+
      '<div class="t">'+nm+'</div>'+
      '<div class="u">@'+(i.username||i.id)+'</div>'+
      '<span class="k">'+i.kind+'</span>'+
      (added.has(u)?'<span class="add2">已添加</span>':'<span class="add2">+ 添加</span>')+
      '</div>';
  }).join('');
}
async function quickAdd(u){
  const body=new URLSearchParams({username:u});
  const r=await fetch('/manage/api/add',{method:'POST',body});
  const d=await r.json(); setMsg(d.msg,d.ok);
  if(d.ok){setTimeout(()=>location.reload(),800);}
}
async function addChan(){
  const v=document.getElementById('newChan').value.trim(); if(!v) return;
  const body=new URLSearchParams({username:v});
  const r=await fetch('/manage/api/add',{method:'POST',body});
  const d=await r.json(); setMsg(d.msg,d.ok); if(d.ok){setTimeout(()=>location.reload(),800);}
}
async function delChannel(u){
  if(!confirm('删除频道 '+u+'?')) return;
  const body=new URLSearchParams({username:u});
  const r=await fetch('/manage/api/del',{method:'POST',body});
  const d=await r.json(); setMsg(d.msg,d.ok); if(d.ok){setTimeout(()=>location.reload(),800);}
}
async function changePw(){
  const kind=document.getElementById('pwKind').value;
  const v=document.getElementById('pwVal').value.trim(); if(!v){setMsg('请输入新密码',false);return;}
  const body=new URLSearchParams({kind:kind,password:v});
  const r=await fetch('/manage/api/password',{method:'POST',body});
  const d=await r.json(); setMsg(d.msg,d.ok);
  if(d.ok){document.getElementById('pwVal').value='';}
}
// 事件委托：mineList 内点击 .mine-item → 读 data-u 添加（避免内联 onclick 引号问题）
document.getElementById('mineList').addEventListener('click',e=>{
  const it=e.target.closest('.mine-item'); if(it&&it.dataset.u) quickAdd(it.dataset.u);
});
</script></body></html>"""

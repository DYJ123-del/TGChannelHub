#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TGChannelHub Uploader — aiohttp 路由（受管理密码保护）"""
import os
import json

from aiohttp import web

from . import UploadEngine

# 上传目录白名单（compose 环境变量 UPLOAD_ROOT，默认容器内 /upload）
UPLOAD_ROOT = os.environ.get("UPLOAD_ROOT", "/upload")

routes = web.RouteTableDef()

# 单例引擎（__main__ 启动时 initialize_engine() 注入 client）
_engine: "UploadEngine" = None


def initialize_engine(client, dests=None):
    global _engine
    if _engine is None:
        _engine = UploadEngine(client, dests=dests)
    return _engine


def get_engine():
    return _engine


# ---------- 鉴权 ----------
def _admin_ok(req: web.Request) -> bool:
    """复用主服务管理密码校验。"""
    from ..channel_ui import _admin_ok as _ui_admin_ok
    return _ui_admin_ok(req)


def _safe_path(p):
    """把用户传入路径限定在 UPLOAD_ROOT 白名单内，返回真实路径；越界返回 None"""
    p = (p or "").strip()
    if not p:
        return UPLOAD_ROOT
    if not os.path.isabs(p):
        p = os.path.join(UPLOAD_ROOT, p)
    root = os.path.realpath(UPLOAD_ROOT)
    real = os.path.realpath(p)
    if real != root and not real.startswith(root + os.sep):
        return None
    return real


# ---------- 页面 ----------
@routes.get("/upload")
async def upload_page(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        from ..channel_ui import ADMIN_LOGIN_HTML
        return web.Response(status=200, content_type="text/html", text=ADMIN_LOGIN_HTML)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html")


# ---------- API ----------
@routes.get("/up/api/state")
async def api_state(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    if _engine is None:
        return web.json_response({"error": "engine not ready"}, status=503)
    await _engine.ensure_workers()  # 自愈：worker 全灭时补足
    return web.json_response({"tasks": _engine.all_tasks(), "summary": _engine.summary()})


@routes.post("/up/api/enqueue")
async def api_enqueue(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    if _engine is None:
        return web.json_response({"error": "engine not ready"}, status=503)
    data = await req.json()
    paths = data.get("files")
    if not isinstance(paths, list):
        return web.json_response({"error": "files 必须是数组"}, status=400)
    paths = [p for p in paths if isinstance(p, str) and p]
    dest = (data.get("dest") or "").strip()
    tag = (data.get("tag") or "").strip()
    skip_done = data.get("skip_done", True)
    if not paths or not dest:
        return web.json_response({"error": "需要 files 和 dest"}, status=400)
    # 白名单校验
    safe_paths = []
    for p in paths:
        rp = _safe_path(p)
        if rp is None:
            return web.json_response({"error": f"路径越界: {p}"}, status=403)
        safe_paths.append(rp)
    added, skipped = await _engine.add_files(safe_paths, dest, tag, skip_done=skip_done)
    return web.json_response({"added": added, "skipped": skipped})


@routes.post("/up/api/pause")
async def api_pause(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    _engine.set_paused(True)
    return web.json_response({"ok": True})


@routes.post("/up/api/resume")
async def api_resume(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    _engine.set_paused(False)
    return web.json_response({"ok": True})


@routes.post("/up/api/cancel")
async def api_cancel(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    data = await req.json()
    ok = await _engine.cancel_task(data.get("id", ""))
    return web.json_response({"ok": ok})


@routes.post("/up/api/cancel_all")
async def api_cancel_all(req: web.Request) -> web.Response:
    """取消全部上传任务（排队+暂停），清空待传持久化。"""
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    cancelled = await _engine.cancel_all()
    return web.json_response({"ok": True, "cancelled": cancelled})


@routes.post("/up/api/clear_done")
async def api_clear_done(req: web.Request) -> web.Response:
    """清除任务列表中已完成的（done/failed），保留 waiting/uploading/paused。"""
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    removed = 0
    for tid in list(_engine.tasks.keys()):
        t = _engine.tasks[tid]
        if t.status in ("done", "failed"):
            _engine.tasks.pop(tid, None)
            removed += 1
    _engine._notify()
    return web.json_response({"ok": True, "removed": removed})


@routes.post("/up/api/retry")
async def api_retry(req: web.Request) -> web.Response:
    """重试失败任务（置回 waiting 重排队，并同步回待传持久化）。"""
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    data = await req.json()
    tid = data.get("id", "")
    t = _engine.tasks.get(tid)
    if not t or t.status != "failed":
        return web.json_response({"error": "仅可重试 failed 任务"}, status=400)
    t.status = "waiting"
    t.attempt = 0
    t.error = ""
    t.sent = 0
    # 同步回待传队列（重试的任务也要能跨重启恢复）
    _engine._pending.append({"path": t.path, "dest": t.dest, "tag": t.tag})
    _engine._save_queue()
    await _engine._queue.put(t)
    _engine._notify()
    return web.json_response({"ok": True})


@routes.post("/up/api/config")
async def api_config(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    data = await req.json()
    if "max_concurrent" in data:
        try:
            await _engine.set_concurrent(int(data["max_concurrent"]))
        except (TypeError, ValueError):
            return web.json_response({"error": "max_concurrent 必须是数字"}, status=400)
    return web.json_response({"ok": True, "max_concurrent": _engine.max_concurrent})


# 目的地列表缓存（get_dialogs 网络请求慢，60s 内复用）
_dests_cache = {"ts": 0, "items": None}


@routes.get("/up/api/dests")
async def api_dests(req: web.Request) -> web.Response:
    """可上传目的地：当前账号的群组 + 频道（复用管理页 dialogs 逻辑）。

    get_dialogs 走代理网络请求较慢（实测 4.7s），结果缓存 60s，页面打开不再卡。
    """
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    import time as _time
    now = _time.time()
    if _dests_cache["items"] is not None and now - _dests_cache["ts"] < 60:
        return web.json_response({"ok": True, "items": _dests_cache["items"],
                                  "total": len(_dests_cache["items"]), "cached": True})
    from ..channel_ui import client as _client
    from ..log import log
    try:
        dialogs = await _client.get_dialogs(limit=200)
    except Exception as e:
        log.warning(f"get_dialogs failed: {e}")
        return web.json_response({"error": f"拉取失败: {e}"}, status=500)
    items = []
    for d in dialogs:
        # 只保留群组/频道/超群，排除私聊、机器人、用户
        if not (d.is_group or d.is_channel):
            continue
        if getattr(d.entity, "bot", False):
            continue
        if not getattr(d.entity, "megagroup", False) and not d.is_channel:
            continue
        ent = d.entity
        user = getattr(ent, "username", None) or ""
        is_group = bool(getattr(ent, "megagroup", False))
        chat_id = getattr(ent, "id", 0)
        # 注意：value 必须保留 -100 前缀（频道/超群的正规 id），否则 _resolve_peer 无法 get_entity
        items.append({
            "name": getattr(ent, "title", None) or user or f"id:{chat_id}",
            "value": user or str(chat_id),
            "kind": "群组" if is_group else "频道",
        })
    items.sort(key=lambda x: (x["kind"], x["name"]))
    _dests_cache["ts"] = _time.time()
    _dests_cache["items"] = items
    return web.json_response({"ok": True, "items": items, "total": len(items)})


@routes.get("/up/api/browse")
async def api_browse(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    real = _safe_path(req.query.get("path", ""))
    if real is None:
        return web.json_response({"error": "路径越界"}, status=403)
    try:
        entries = sorted(os.scandir(real), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    dirs, files = [], []
    for e in entries:
        try:
            if e.is_dir():
                dirs.append({"name": e.name, "path": e.path})
            else:
                st = e.stat()
                files.append({"name": e.name, "path": e.path, "size": st.st_size})
        except OSError:
            continue
    return web.json_response({"path": real, "root": UPLOAD_ROOT,
                              "dirs": dirs, "files": files})


@routes.post("/up/api/watch")
async def api_watch(req: web.Request) -> web.Response:
    """多 watcher 管理。
    body: {action: "add"|"update"|"remove"|"clear", wid?, dir?, dest?, tag?, interval?}
    """
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    data = await req.json()
    action = data.get("action", "add")
    if action in ("add", "update"):
        real = _safe_path(data.get("dir", ""))
        if real is None:
            return web.json_response({"error": "路径越界"}, status=403)
        dest = (data.get("dest") or "").strip()
        tag = (data.get("tag") or "").strip()
        try:
            interval = float(data.get("interval", 30))
        except (TypeError, ValueError):
            return web.json_response({"error": "interval 必须是数字"}, status=400)
        wid = await _engine.watch_start(real, interval=interval, dest=dest, tag=tag,
                                        wid=data.get("wid"))
        return web.json_response({"ok": True, "wid": wid, "watch": _engine.watch_list()})
    elif action == "remove":
        await _engine.watch_stop(wid=data.get("wid"))
        return web.json_response({"ok": True, "watch": _engine.watch_list()})
    elif action == "clear":
        await _engine.watch_stop(wid=None)
        return web.json_response({"ok": True, "watch": []})
    return web.json_response({"error": f"未知 action: {action}"}, status=400)


@routes.get("/up/api/dirs")
async def api_dirs(req: web.Request) -> web.Response:
    """可监控目录列表：UPLOAD_ROOT 下 2 层内的用户目录（供下拉选择）。

    排除隐藏/系统目录（.@# 开头、@apphome、trash、docker、@appshare 等），
    避免把 NAS 全量几十万目录灌进下拉。
    """
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    root = os.path.realpath(UPLOAD_ROOT)
    skip_prefixes = (".", "@apphome", "@appshare", "docker", "trash", "lost+found", ".snapshot")
    out = []
    try:
        for name in sorted(os.listdir(root)):
            if name.startswith(skip_prefixes) or name in ("docker",):
                continue
            p1 = os.path.join(root, name)
            if not os.path.isdir(p1):
                continue
            out.append({"path": p1, "label": name})
            # 第二层
            try:
                for name2 in sorted(os.listdir(p1)):
                    if name2.startswith(skip_prefixes):
                        continue
                    p2 = os.path.join(p1, name2)
                    if os.path.isdir(p2):
                        out.append({"path": p2, "label": f"{name}/{name2}"})
            except OSError:
                pass
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "root": UPLOAD_ROOT, "dirs": out})

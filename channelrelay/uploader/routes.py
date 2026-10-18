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
    return web.json_response({"tasks": _engine.all_tasks(), "summary": _engine.summary()})


@routes.post("/up/api/enqueue")
async def api_enqueue(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    if _engine is None:
        return web.json_response({"error": "engine not ready"}, status=503)
    data = await req.json()
    paths = [p for p in (data.get("files") or []) if p]
    dest = (data.get("dest") or "").strip()
    tag = (data.get("tag") or "").strip()
    if not paths or not dest:
        return web.json_response({"error": "需要 files 和 dest"}, status=400)
    # 白名单校验
    safe_paths = []
    for p in paths:
        rp = _safe_path(p)
        if rp is None:
            return web.json_response({"error": f"路径越界: {p}"}, status=403)
        safe_paths.append(rp)
    added = await _engine.add_files(safe_paths, dest, tag)
    return web.json_response({"added": added})


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


@routes.post("/up/api/config")
async def api_config(req: web.Request) -> web.Response:
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    data = await req.json()
    if "max_concurrent" in data:
        await _engine.set_concurrent(int(data["max_concurrent"]))
    return web.json_response({"ok": True, "max_concurrent": _engine.max_concurrent})


@routes.get("/up/api/dests")
async def api_dests(req: web.Request) -> web.Response:
    """可上传目的地：当前账号的群组 + 频道（复用管理页 dialogs 逻辑）"""
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    from ..channel_ui import client as _client
    from ..log import log
    try:
        dialogs = await _client.get_dialogs(limit=200)
    except Exception as e:
        log.warning(f"get_dialogs failed: {e}")
        return web.json_response({"error": f"拉取失败: {e}"}, status=500)
    items = []
    for d in dialogs:
        ent = d.entity
        user = getattr(ent, "username", None) or ""
        is_group = bool(getattr(ent, "megagroup", False))
        chat_id = getattr(ent, "id", 0)
        if chat_id and str(chat_id).startswith("-100"):
            chat_id = int(str(chat_id)[4:])
        items.append({
            "name": getattr(ent, "title", None) or user or f"id:{chat_id}",
            "value": user or str(chat_id),
            "kind": "群组" if is_group else "频道",
        })
    items.sort(key=lambda x: (x["kind"], x["name"]))
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
    if not _admin_ok(req):
        return web.json_response({"error": "auth"}, status=401)
    data = await req.json()
    if data.get("enabled"):
        real = _safe_path(data.get("dir", ""))
        if real is None:
            return web.json_response({"error": "路径越界"}, status=403)
        dest = (data.get("dest") or "").strip()
        tag = (data.get("tag") or "").strip()
        interval = float(data.get("interval", 30))
        await _engine.watch_start(real, interval=interval, dest=dest, tag=tag)
        return web.json_response({"ok": True, "watch": _engine.summary()["watch"]})
    else:
        _engine.watch_stop()
        return web.json_response({"ok": True, "watch": _engine.summary()["watch"]})

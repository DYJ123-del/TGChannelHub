# channelrelay - Web-based Telegram channel video relay (fork of tgfilestream by Tulir Asokan, AGPL-3.0)
# Copyright (C) 2019 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import asyncio
import sys

from aiohttp import web
from telethon import functions

from .telegram import client, transfer
from .channel_ui import routes as ui_routes
from .uploader.routes import routes as uploader_routes
from .uploader import UploadEngine
from .setup_ui import _authorized, routes as setup_routes
from .config import host, port, public_url, tg_bot_token
from .log import log

# 未登录重定向 middleware：Telegram session 未认证时，除 /setup 外全部访问 302 到引导页
@web.middleware
async def _setup_middleware(req: web.Request, handler):
    path = req.path
    if path.startswith("/setup"):
        return await handler(req)
    if path in ("/favicon.ico", "/robots.txt"):
        return await handler(req)
    if not await _authorized():
        # 防中间代理缓存 302 重定向（客户端会一直卡在 /setup）
        return web.Response(status=302, headers={"Location": "/setup", "Cache-Control": "no-store"})
    return await handler(req)

server = web.Application(middlewares=[_setup_middleware])
# 原版无鉴权路由（/{id}/{name} 直链流）已弃用——存在未授权访问与 Content-Range off-by-one 问题，
# 全部播放/下载走 channel_ui（/s/ 等，带分享密码鉴权）。不注册原版 routes。
server.add_routes(ui_routes)
server.add_routes(uploader_routes)
server.add_routes(setup_routes)

# 上传引擎单例（注入主 client，启动时开始 worker；watch 由页面 API 控制）
upload_engine: UploadEngine = None
runner = web.AppRunner(server)

loop = asyncio.get_event_loop()


async def start() -> None:
    # 登录容错：已登录/配了 bot token 则正常 start；未登录且无 bot token 时，
    # Telethon 默认走 input() 交互（Docker 无 TTY 会 EOFError 崩溃）——捕获后
    # 仅保持连接，登录完全交给 /setup 引导页或 scripts/login.py。
    try:
        await client.start(bot_token=tg_bot_token)
    except Exception as e:
        log.warning(f"client.start() skipped (not logged in): {e} — 用 /setup 或 scripts/login.py 登录")
    if not client.is_connected():
        try:
            await client.connect()
        except Exception as e:
            log.error(f"Telegram connect failed: {e}")

    config = await client(functions.help.GetConfigRequest())
    for option in config.dc_options:
        if option.ip_address == client.session.server_address:
            if client.session.dc_id != option.id:
                log.warning(f"Fixed DC ID in session from {client.session.dc_id} to {option.id}")
            client.session.set_dc(option.id, option.ip_address, option.port)
            client.session.save()
            break
    transfer.post_init()

    # 初始化上传引擎并启动 worker
    from .uploader.routes import initialize_engine
    global upload_engine
    upload_engine = initialize_engine(client)
    await upload_engine.start()
    log.info("Upload engine started")

    # 预热 DC 连接：启动时建立主 DC 连接池，避免首次播放等待 2-4s 建连（启动慢根因）
    try:
        await transfer.dc_managers[client.session.dc_id]._new_connection()
        log.info("DC connection warmed up (dc%d)" % client.session.dc_id)
    except Exception as e:
        log.warning(f"DC warmup failed: {e}")

    await runner.setup()
    await web.TCPSite(runner, host, port).start()


async def stop() -> None:
    await runner.cleanup()
    await client.disconnect()


try:
    loop.run_until_complete(start())
except Exception:
    log.fatal("Failed to initialize", exc_info=True)
    sys.exit(2)

log.info("Initialization complete")
log.debug(f"Listening at http://{host}:{port}")
log.debug(f"Public URL prefix is {public_url}")

try:
    loop.run_forever()
except KeyboardInterrupt:
    loop.run_until_complete(stop())
except Exception:
    log.fatal("Fatal error in event loop", exc_info=True)
    sys.exit(3)

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
from .config import host, port, public_url, tg_bot_token
from .log import log

server = web.Application()
# 原版无鉴权路由（/{id}/{name} 直链流）已弃用——存在未授权访问与 Content-Range off-by-one 问题，
# 全部播放/下载走 channel_ui（/s/ 等，带分享密码鉴权）。不注册原版 routes。
server.add_routes(ui_routes)
runner = web.AppRunner(server)

loop = asyncio.get_event_loop()


async def start() -> None:
    await client.start(bot_token=tg_bot_token)

    config = await client(functions.help.GetConfigRequest())
    for option in config.dc_options:
        if option.ip_address == client.session.server_address:
            if client.session.dc_id != option.id:
                log.warning(f"Fixed DC ID in session from {client.session.dc_id} to {option.id}")
            client.session.set_dc(option.id, option.ip_address, option.port)
            client.session.save()
            break
    transfer.post_init()

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

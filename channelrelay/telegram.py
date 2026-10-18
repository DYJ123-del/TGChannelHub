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
# along with this program.  If not, see <https://www.gnu.org/licenses/>.'
import logging

from telethon import TelegramClient, events

from .paralleltransfer import ParallelTransferrer
from .config import (
    session_name,
    api_id,
    api_hash,
    public_url,
    start_message,
    group_chat_message
)
from .util import pack_id, get_file_name

log = logging.getLogger(__name__)

import os
try:
    proxy_type = os.environ.get('TG_PROXY_TYPE', 'http')  # http 或 socks5
    proxy_cfg = dict(proxy_type=proxy_type, addr=os.environ['TG_PROXY_HOST'], port=int(os.environ.get('TG_PROXY_PORT','7890')), rdns=True)
    if os.environ.get('TG_PROXY_USER'):
        proxy_cfg['username'] = os.environ['TG_PROXY_USER']
    if os.environ.get('TG_PROXY_PASS'):
        proxy_cfg['password'] = os.environ['TG_PROXY_PASS']
    proxy = proxy_cfg
except KeyError:
    proxy = None
client = TelegramClient(session_name, api_id, api_hash, proxy=proxy)
transfer = ParallelTransferrer(client)

# 收藏夹/私聊监控已关闭（用户不需要"转发文件到收藏夹→回链接"功能）
# 原 handle_message 已移除；如需恢复请取消注释下面代码
# @client.on(events.NewMessage)
# async def handle_message(evt: events.NewMessage.Event) -> None:
#     if not evt.is_private:
#         return
#     if not evt.file:
#         await evt.reply(start_message)
#         return
#     url = public_url / str(pack_id(evt)) / get_file_name(evt)
#     await evt.reply(f"Link to download file: [{url}]({url})")
#     log.info(f"Replied with link for {evt.id} to {evt.from_id} in {evt.chat_id}")

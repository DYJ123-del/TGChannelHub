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
from typing import Union, AsyncGenerator, AsyncContextManager, Dict, Optional, List
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import asyncio
import math
import os
import re
import time
from collections import OrderedDict

from telethon import TelegramClient, utils
from telethon.crypto import AuthKey
from telethon.network import MTProtoSender
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import GetFileRequest
from telethon.tl.types import (Document, InputFileLocation, InputDocumentFileLocation,
                               InputPhotoFileLocation, InputPeerPhotoFileLocation, DcOption)
from telethon.errors import DcIdInvalidError, FileReferenceExpiredError, FileReferenceInvalidError
import telethon.functions as functions
from telethon.tl.alltlobjects import LAYER

from .config import connection_limit

TypeLocation = Union[Document, InputDocumentFileLocation, InputPeerPhotoFileLocation,
                     InputFileLocation, InputPhotoFileLocation]

root_log = logging.getLogger(__name__)

# 全账号级限流闸门：Telegram 对免费账号的文件下载会返回 FloodPremiumWaitError（等 8~15s）。
# 关键语义参考官方 tgnet（ConnectionsManager.cpp）：遇到 FLOOD_WAIT_/FLOOD_PREMIUM_WAIT_ 时
# `discardResponse = true` + 把该请求的 minStartTime 推到 now+wait，即「重新排队等 N 秒」，
# **上层永远看不到失败** —— 所以客户端只会变慢、不会播放失败。
# 我们照此实现：限流不算失败、不计入重试预算、永不因限流中断流；只按 DC 隔离地推迟发送。
# 限流处理：收到 FloodPremiumWait 就按服务端给的秒数等待后重发同一分块，
# 不计入重试预算、不中断流（参考官方 tgnet：限流表现为变慢，不是播放失败）。


class _FileMigrated(Exception):
    """文件实际在另一个 DC（FILE_MIGRATE_X）：必须换 DC 重下，参考 Nagram FileLoadOperation:2117。"""
    def __init__(self, new_dc: int):
        self.new_dc = new_dc
        super().__init__(f"migrate to DC {new_dc}")


class _ConnDead(Exception):
    """当前连接已不可用（端口断开/连续超时）：丢弃这条连接换一条继续，不能原地重试。"""


class _NeedRefRefresh(Exception):
    """file_reference 过期/失效：通知上层重取消息（Telethon 重取会刷新引用）。"""


class _EndEarly(Exception):
    """OFFSET_INVALID：服务端说越界 → 视为文件到此结束，正常收尾而不是报错。"""


class _LimitInvalid(Exception):
    """LIMIT_INVALID：分块尺寸被服务端拒绝 → 降级到更小分块重试（参考 Nagram :2106）。"""


def _env_num(name: str, default, cast=float):
    """读环境变量数字：非法值回退默认（避免 compose 写错一个字符导致容器起不来）。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return cast(str(raw).strip())
    except Exception:
        root_log.warning("环境变量 %s=%r 非法，回退默认 %r", name, raw, default)
        return default


async def _drop_tasks(pending: Dict[int, asyncio.Task]) -> None:
    """取消并回收在途分块任务。

    只 cancel 不 await：已完成（含异常）的 Task 取消是 no-op，其异常永远不会被取回
    （日志刷 "Task exception was never retrieved"），且取消不会立刻生效。
    """
    tasks = list(pending.values())
    pending.clear()
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


FLOOD_MAX_WAIT = _env_num("TG_FLOOD_MAX_WAIT", 60.0)           # 单次限流最长等待（秒）

# 内存分块缓存（LRU，不落盘）：播放器重试/回拖/多设备看同一段时不再重复打 TG，
# 直接减少请求量 → 少撞 FloodPremiumWait。key=(packed消息id, 分块大小, 分块序号)。
_part_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_part_cache_bytes = 0
PART_CACHE_MAX = _env_num("TG_PART_CACHE_MB", 128.0) * 1024 * 1024


def _cache_get(key):
    data = _part_cache.get(key)
    if data is not None:
        _part_cache.move_to_end(key)
    return data


def _cache_put(key, data: bytes) -> None:
    global _part_cache_bytes
    if PART_CACHE_MAX <= 0 or not data or len(data) > PART_CACHE_MAX:
        return
    old = _part_cache.pop(key, None)
    if old is not None:
        _part_cache_bytes -= len(old)
    _part_cache[key] = data
    _part_cache_bytes += len(data)
    while _part_cache_bytes > PART_CACHE_MAX and _part_cache:
        _k, _v = _part_cache.popitem(last=False)
        _part_cache_bytes -= len(_v)

if connection_limit > 25:
    root_log.warning("The connection limit should not be set above 25 to avoid"
                     " infinite disconnect/reconnect loops")


@dataclass
class Connection:
    log: logging.Logger
    sender: MTProtoSender
    lock: asyncio.Lock
    users: int = 0
    fails: int = 0        # 连续失败次数（超时/连接错误）
    dead: bool = False    # 判定死亡，下次取连接时剔除


class DCConnectionManager:
    log: logging.Logger
    client: TelegramClient
    loop: asyncio.AbstractEventLoop

    dc_id: int
    dc: Optional[DcOption]
    auth_key: Optional[AuthKey]
    connections: List[Connection]

    _list_lock: asyncio.Lock

    def __init__(self, client: TelegramClient, dc_id: int) -> None:
        self.log = root_log.getChild(f"dc{dc_id}")
        self.client = client
        self.dc_id = dc_id
        self.auth_key = None
        self.connections = []
        self._list_lock = asyncio.Lock()
        self.loop = client.loop
        self.dc = None

    async def _new_connection(self) -> Connection:
        if not self.dc:
            self.dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        index = len(self.connections) + 1
        conn = Connection(sender=sender, log=self.log.getChild(f"conn{index}"), lock=asyncio.Lock())
        async with conn.lock:
            conn.log.info("Connecting...")
            connection_info = self.client._connection(self.dc.ip_address, self.dc.port, self.dc.id,
                                                      loggers=self.client._log,
                                                      proxy=self.client._proxy)
            try:
                await sender.connect(connection_info)
                if not self.auth_key:
                    await self._export_auth_key(conn)
            except Exception:
                # 建连/导出授权失败：不留半死连接（否则 _next_connection 会挑中它，后续全部分块立刻失败）
                try:
                    await sender.disconnect()
                except Exception:
                    pass
                conn.log.warning("建连失败，已丢弃该连接", exc_info=True)
                raise
            # 连成功才登记，避免 others 拿到未就绪的连接
            self.connections.append(conn)
            return conn

    async def _export_auth_key(self, conn: Connection) -> None:
        self.log.info(f"Exporting auth to DC {self.dc.id}"
                      f" (main client is in {self.client.session.dc_id})")
        try:
            auth = await self.client(ExportAuthorizationRequest(self.dc.id))
        except DcIdInvalidError:
            self.log.debug("Got DcIdInvalidError")
            self.auth_key = self.client.session.auth_key
            conn.sender.auth_key = self.auth_key
            return
        # 新版 Telethon：用 _init_request + InvokeWithLayerRequest 发送导入授权
        self.client._init_request.query = ImportAuthorizationRequest(
            id=auth.id, bytes=auth.bytes
        )
        req = functions.InvokeWithLayerRequest(LAYER, self.client._init_request)
        await conn.sender.send(req)
        self.auth_key = conn.sender.auth_key

    async def _next_connection(self) -> Connection:
        # 先清理已判死的连接（关闭 socket，避免它继续被挑中去拉数据）
        for conn in list(self.connections):
            if conn.dead and conn.users <= 0:
                self.connections.remove(conn)
                try:
                    await conn.sender.disconnect()
                except Exception:
                    pass
                conn.log.warning("剔除失效连接，剩余 %d 条", len(self.connections))
        best_conn: Optional[Connection] = None
        for conn in self.connections:
            if conn.dead:
                continue
            if not best_conn or conn.users < best_conn.users:
                best_conn = conn
        if (not best_conn or best_conn.users > 0) and (
                len(self.connections) < connection_limit or best_conn is None):
            best_conn = await self._new_connection()
        return best_conn

    @asynccontextmanager
    async def get_connection(self) -> AsyncContextManager[Connection]:
        async with self._list_lock:
            conn: Connection = await asyncio.shield(self._next_connection())
            # The connection is locked so reconnections don't stack
            async with conn.lock:
                conn.users += 1
        try:
            yield conn
        finally:
            conn.users -= 1


class ParallelTransferrer:
    log: logging.Logger = logging.getLogger(__name__)
    client: TelegramClient
    loop: asyncio.AbstractEventLoop

    dc_managers: Dict[int, DCConnectionManager]

    _counter: int

    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.loop = self.client.loop
        self._counter = 0
        self.dc_managers = {
            1: DCConnectionManager(client, 1),
            2: DCConnectionManager(client, 2),
            3: DCConnectionManager(client, 3),
            4: DCConnectionManager(client, 4),
            5: DCConnectionManager(client, 5),
        }

    def post_init(self) -> None:
        self.dc_managers[self.client.session.dc_id].auth_key = self.client.session.auth_key

    async def warm(self, dc_id: int, count: int = 4) -> None:
        """预热目标 DC 的连接池：默认建 4 条。

        为什么是 4：浏览器播放器会**并发**发 3~4 个 Range 请求（整片 + 尾部 moov 探测 + 正式播放），
        每条下载流占用一条连接；连接不足时请求互相排队，首字节被饿死（实测 0 字节 4~15 秒）。
        实测：2 条连接时首帧 9.7s，4 条后 3.1s。
        """
        dcm = self.dc_managers.get(dc_id)
        if not dcm:
            return
        want = max(1, min(count, connection_limit))
        try:
            async with dcm._list_lock:
                while len(dcm.connections) < want:
                    await dcm._new_connection()
        except Exception:
            self.log.warning("warm DC %s failed", dc_id, exc_info=True)

    @property
    def next_index(self) -> int:
        self._counter += 1
        return self._counter

    # 并发在途分块数（这就是「多线程加速」的全部：512KB × N 并发拉）
    CONCURRENCY = max(1, min(32, _env_num("TG_PARALLEL_CONCURRENCY", 8, int)))
    # 偶发 RPC 异常的重试次数（限流另算，不占这个预算）
    PART_RETRIES = max(1, _env_num("TG_PART_RETRIES", 2, int))

    async def _fetch_part(self, conn, location, part: int, part_size: int, log, dc_id: int,
                          cache_key=None) -> bytes:
        """取单个分块：过全局限流闸门 → 请求。

        与官方 tgnet 一致：限流(420/FloodPremiumWait)**永远不算失败**——只把该 DC 的闸门往后推，
        然后重新排队等；不计入重试预算、不设总等待上限（只有客户端断开才会结束）。所以表现为
        「播放变慢/转圈」，而不是「播放失败」。
        其它错误（超时、RPC 异常）才按 PART_RETRIES 重试，耗尽才中断。
        """
        last_err: Optional[BaseException] = None
        attempt = 0
        ck = (cache_key, part_size, part) if cache_key is not None else None
        if ck is not None:
            hit = _cache_get(ck)
            if hit is not None:
                log.debug(f"Part {part} 命中内存缓存")
                return hit
        while True:
            req = GetFileRequest(location, offset=part * part_size, limit=part_size)
            try:
                # 不加 per-part 超时：限流态下单块 512KB 可能要 15~20s，
                # 超时会触发重试/换连接风暴，越重试越慢（原始版本就是无限等）。
                res = await conn.sender.send(req)
                if ck is not None:
                    _cache_put(ck, res.bytes)
                conn.fails = 0
                return res.bytes
            except Exception as e:  # FloodPremiumWaitError / RPC error / 连接被重置 / 迁移
                msg_text = getattr(e, "message", "") or ""
                # ⓪ 连接已断（telethon 在断连时同步抛 ConnectionError）：换连接，不烧重试预算
                if isinstance(e, ConnectionError) or not getattr(conn.sender, "_user_connected", True):
                    conn.dead = True
                    log.warning(f"Part {part} 连接已断开（{type(e).__name__}），换连接续传")
                    raise _ConnDead() from e
                # ① FILE_MIGRATE_X：文件被迁到别的 DC → 换 DC 重下（不能当普通错误重试旧 DC）
                if "FILE_MIGRATE_" in msg_text:
                    digits = ""
                    for ch in msg_text.split("FILE_MIGRATE_", 1)[1]:
                        if ch.isdigit():
                            digits += ch
                        else:
                            break
                    if digits:
                        raise _FileMigrated(int(digits))
                # ② file_reference 过期/失效 → 让上层刷新消息后重取
                if isinstance(e, (FileReferenceExpiredError, FileReferenceInvalidError)):
                    raise _NeedRefRefresh()
                # ③ OFFSET_INVALID → 文件比声明的短，正常收尾
                if "OFFSET_INVALID" in msg_text:
                    raise _EndEarly()
                # ④ LIMIT_INVALID → 分块尺寸被拒，交给上层降级重试
                if "LIMIT_INVALID" in msg_text:
                    raise _LimitInvalid()
                wait = getattr(e, "seconds", None) or 0
                if not wait:
                    # 420 有时以未映射的 RPCError 形式出现（无 .seconds），靠文本兜底
                    mf = re.search(r"FLOOD(?:_PREMIUM)?_WAIT_(\d+)", msg_text)
                    if mf:
                        wait = int(mf.group(1))
                if wait:
                    # 限流：睡够服务端要求的秒数后重发同一分块，不计 attempt、不中断流
                    last_err = e
                    capped = min(float(wait), FLOOD_MAX_WAIT)
                    log.info(f"Part {part} 限流 {type(e).__name__} {wait}s → 等待后重试（不失败）")
                    await asyncio.sleep(capped)
                    continue
                last_err = e
                attempt += 1
                log.warning(f"Part {part} 失败: {type(e).__name__} {msg_text[:60]} "
                            f"(attempt {attempt}/{self.PART_RETRIES})")
            if attempt >= self.PART_RETRIES:
                raise last_err or RuntimeError(f"part {part} download failed")
            await asyncio.sleep(min(2 ** (attempt - 1), 4))

    async def _int_download(self, request: GetFileRequest, first_part: int, last_part: int,
                            part_count: int, part_size: int, dc_id: int, first_part_cut: int,
                            last_part_cut: int, refresh_cb=None, cache_key=None) -> AsyncGenerator[bytes, None]:
        log = self.log
        # state 可变：FILE_MIGRATE 换 DC / file_reference 刷新后要改 location 与 dc_id
        state = {"location": request.location, "dc_id": dc_id, "refreshed": False,
                 "migrations": 0, "conn_dead": 0}
        pending: Dict[int, asyncio.Task] = {}
        next_yield = first_part
        try:
            while True:  # 迁移/刷新后重新进入，next_yield 保留（已 yield 的块不重发）
                dcm = self.dc_managers[state["dc_id"]]
                try:
                    async with dcm.get_connection() as conn:
                        log = conn.log
                        next_send = next_yield

                        def _launch(part: int):
                            pending[part] = asyncio.ensure_future(
                                self._fetch_part(conn, state["location"], part, part_size, log,
                                                 state["dc_id"], cache_key))

                        # 直接拉满并发（不做慢启动：慢启动会让每个新流开头的吞吐只有 1/8，
                        # 实测体验是「点开要等几秒才流畅」；直接并发反而更快）
                        while next_send <= last_part and len(pending) < self.CONCURRENCY:
                            _launch(next_send)
                            next_send += 1

                        while next_yield <= last_part:
                            fut = pending.pop(next_yield)
                            data = await fut
                            # Range 首尾都要裁：单分块 Range（first_part == last_part）时两个 cut 都要生效
                            if first_part == last_part:
                                data = data[first_part_cut:last_part_cut]
                            elif next_yield == first_part:
                                data = data[first_part_cut:]
                            elif next_yield == last_part:
                                data = data[:last_part_cut]
                            log.debug(f"Part {next_yield}/{last_part} (total {part_count}) downloaded")
                            while next_send <= last_part and len(pending) < self.CONCURRENCY:
                                _launch(next_send)
                                next_send += 1
                            yield data
                            next_yield += 1
                        log.debug("Parallel download finished")
                        return
                except _FileMigrated as mig:
                    await _drop_tasks(pending)
                    if mig.new_dc == state["dc_id"]:
                        raise
                    state["migrations"] += 1
                    if state["migrations"] > 2:
                        log.warning("FILE_MIGRATE 反复出现（%d 次），放弃以免来回换 DC 死循环",
                                    state["migrations"])
                        raise
                    log.warning("FILE_MIGRATE: DC%s → DC%s，换 DC 继续下载",
                                state["dc_id"], mig.new_dc)
                    state["dc_id"] = mig.new_dc
                    continue
                except _ConnDead:
                    await _drop_tasks(pending)
                    state["conn_dead"] += 1
                    if state["conn_dead"] > 5:
                        log.warning("连续 %d 条连接失效，放弃该流", state["conn_dead"])
                        raise
                    log.warning("连接失效，换一条连接从 Part %s 继续", next_yield)
                    continue
                except _NeedRefRefresh:
                    await _drop_tasks(pending)
                    if refresh_cb is None or state["refreshed"]:
                        raise
                    state["refreshed"] = True
                    log.warning("file_reference 过期，刷新消息后继续下载")
                    new_media = await refresh_cb()
                    if new_media is None:
                        raise
                    state["dc_id"], state["location"] = utils.get_input_location(new_media)
                    continue
                except _EndEarly:
                    await _drop_tasks(pending)
                    log.info("OFFSET_INVALID：服务端声明越界，按文件结束处理")
                    return
        except (GeneratorExit, StopAsyncIteration, asyncio.CancelledError):
            for t in pending.values():
                t.cancel()
            log.debug("Parallel download interrupted, cancelled %d pending", len(pending))
            raise
        except Exception:
            await _drop_tasks(pending)
            # 重试耗尽才走到这里：必须向上抛，让客户端看到连接中断（而不是静默截断成一个卡死的 206）
            log.warning("Parallel download failed after retries", exc_info=True)
            raise

    # 分块尺寸阶梯：512KB 被服务端拒（LIMIT_INVALID）时降一档重试（参考 Nagram :2106）
    PART_SIZES = (512 * 1024, 128 * 1024)

    async def download(self, file: TypeLocation, file_size: int, offset: int, limit: int,
                       refresh_cb=None, cache_key=None) -> AsyncGenerator[bytes, None]:
        yielded = False
        if limit <= offset:
            return
        for idx, part_size in enumerate(self.PART_SIZES):
            dc_id, location = utils.get_input_location(file)
            # limit 是「独占 end」（aiohttp http_range.stop / 文件大小），
            # 所以最后一个字节索引是 limit-1：多算一块会让每个 Range 白拉 512KB 的 TG 数据
            # （旧实现 ceil(limit/ps) + 依赖 aiohttp 按 Content-Length 裁掉多余字节）。
            first_part = offset // part_size
            first_part_cut = offset % part_size
            last_part = (limit - 1) // part_size
            last_part_cut = (limit - 1) % part_size + 1
            part_count = math.ceil(file_size / part_size)
            self.log.debug(f"Starting parallel download: chunks {first_part}-{last_part}"
                           f" of {part_count} {location!s} (part={part_size})")
            request = GetFileRequest(location, offset=first_part * part_size, limit=part_size)
            try:
                async for chunk in self._int_download(
                        request, first_part, last_part, part_count, part_size, dc_id,
                        first_part_cut, last_part_cut, refresh_cb=refresh_cb,
                        cache_key=cache_key):
                    yielded = True
                    yield chunk
                return
            except _LimitInvalid:
                # 只有「一个字节都还没发出」时才能换分块重来；已发出的不能再回头
                if yielded or idx == len(self.PART_SIZES) - 1:
                    raise
                self.log.warning("LIMIT_INVALID → 分块 %d 降级为 %d 重试",
                                 part_size, self.PART_SIZES[idx + 1])

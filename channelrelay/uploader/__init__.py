#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TGChannelHub Uploader — 上传子模块（内嵌于主服务）

将本地文件批量上传到 Telegram 频道/群组（个人网盘）。
复用主进程的 Telethon client（单实例，无 session 锁冲突）。

功能:
- 目录浏览器选文件/文件夹
- 队列：并发数可调、暂停/继续、单任务取消
- 并行分块加速上传（512KB × N）
- watch 目录：新文件自动入队
"""
import asyncio
import os
import random
import time
from collections import OrderedDict

from telethon import TelegramClient
from telethon.tl import types, functions
from telethon.tl.types import InputPeerChannel

PART_SIZE = 512 * 1024  # 512KB，Telegram 官方推荐最大 part
DEFAULT_PARALLEL = 8    # 单文件分块并发数


class Task:
    """单个上传任务"""
    STATUS_WAIT = "waiting"
    STATUS_RUN = "uploading"
    STATUS_DONE = "done"
    STATUS_FAIL = "failed"
    STATUS_PAUSED = "paused"

    def __init__(self, path, dest, tag="", task_id=None):
        self.id = task_id or f"t{int(time.time()*1000)}{random.randint(1000,9999)}"
        self.path = path
        self.name = os.path.basename(path)
        self.size = os.path.getsize(path)
        self.dest = dest
        self.tag = tag
        self.status = Task.STATUS_WAIT
        self.sent = 0
        self.speed = 0.0
        self.error = ""
        self.elapsed = 0.0
        self._started_at = 0.0
        self._last_tick = 0.0
        self._last_sent = 0
        self.parallel = DEFAULT_PARALLEL

    def to_json(self):
        return {
            "id": self.id, "path": self.path, "name": self.name,
            "size": self.size, "dest": self.dest, "tag": self.tag,
            "status": self.status, "sent": self.sent, "speed": round(self.speed, 1),
            "pct": round(self.sent * 100 / self.size, 1) if self.size else 0,
            "error": self.error, "elapsed": round(self.elapsed, 1),
        }


class UploadEngine:
    """上传引擎：注入 client，队列 + 并发 worker + 并行分块 + watch"""

    def __init__(self, client: TelegramClient, dests=None):
        self.client = client
        self.tasks = OrderedDict()          # id -> Task
        self.max_concurrent = 3             # 同时上传几个文件
        self._queue = asyncio.Queue()
        self._workers = []
        self._paused = False
        self._running = False
        self._watch_task = None
        self._watch_dir = ""
        self._watch_interval = 30.0
        self._watch_dest = ""
        self._watch_tag = ""
        self._watch_seen = {}               # path -> (size, mtime)
        self._dests = dests or []
        # 事件回调（路由层注册，进度变化时通知）
        self.on_change = None

    # ---------- 状态 ----------
    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def all_tasks(self):
        return [t.to_json() for t in self.tasks.values()]

    def summary(self):
        st = {}
        for t in self.tasks.values():
            st[t.status] = st.get(t.status, 0) + 1
        return {
            "total": len(self.tasks), "by_status": st,
            "max_concurrent": self.max_concurrent, "paused": self._paused,
            "watch": {"dir": self._watch_dir, "interval": self._watch_interval,
                      "active": self._watch_task is not None},
            "dests": self._dests,
        }

    # ---------- 队列控制 ----------
    async def start(self):
        if self._running:
            return
        self._running = True
        for _ in range(self.max_concurrent):
            w = asyncio.create_task(self._worker())
            self._workers.append(w)

    async def stop(self):
        self._running = False
        for w in self._workers:
            w.cancel()
        self._workers = []
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None

    async def add_files(self, paths, dest, tag=""):
        """批量加入任务（paths 可为目录，递归收集）"""
        files = []
        for p in paths:
            if os.path.isdir(p):
                for root, _, fnames in os.walk(p):
                    for fn in sorted(fnames):
                        fp = os.path.join(root, fn)
                        if os.path.isfile(fp):
                            files.append(fp)
            elif os.path.isfile(p):
                files.append(p)
        added = 0
        for f in files:
            t = Task(f, dest, tag)
            self.tasks[t.id] = t
            await self._queue.put(t)
            added += 1
        self._notify()
        return added

    async def set_concurrent(self, n):
        """动态调整并发数（目标 worker 数）"""
        n = max(1, min(int(n), 16))
        self.max_concurrent = n
        cur = len(self._workers)
        if cur < n and self._running:
            for _ in range(n - cur):
                self._workers.append(asyncio.create_task(self._worker()))
        elif cur > n:
            # 只保留 n 个 worker；多余的等当前任务结束自然退出
            for w in self._workers[n:]:
                w.cancel()
            self._workers = self._workers[:n]
        self._notify()

    def set_paused(self, paused):
        self._paused = paused
        self._notify()

    async def cancel_task(self, tid):
        t = self.tasks.get(tid)
        if t and t.status in (Task.STATUS_WAIT, Task.STATUS_PAUSED):
            t.status = "cancelled"
            self.tasks.pop(tid, None)
            self._notify()
            return True
        return False

    # ---------- worker ----------
    async def _worker(self):
        while self._running:
            t = await self._queue.get()
            if t.status == "cancelled" or t.id not in self.tasks:
                continue
            # 暂停等待
            while self._paused and self._running:
                if t.status != Task.STATUS_PAUSED:
                    t.status = Task.STATUS_PAUSED
                    self._notify()
                await asyncio.sleep(0.5)
            if not self._running:
                break
            t.status = Task.STATUS_RUN
            t._started_at = time.time()
            t._last_tick = time.time()
            t._last_sent = 0
            self._notify()
            try:
                ok, err = await self._upload_file(t)
                if ok:
                    t.status = Task.STATUS_DONE
                else:
                    t.status = Task.STATUS_FAIL
                    t.error = err
                t.elapsed = time.time() - t._started_at
            except asyncio.CancelledError:
                t.status = Task.STATUS_PAUSED
                self._notify()
                raise
            except Exception as e:
                t.status = Task.STATUS_FAIL
                t.error = str(e)
            self._notify()

    # ---------- 并行分块上传 ----------
    async def _progress_cb(self, t, received, total):
        t.sent = received
        now = time.time()
        dt = now - t._last_tick
        if dt >= 0.5:
            t.speed = (received - t._last_sent) / dt / 1024 / 1024
            t._last_tick = now
            t._last_sent = received
            self._notify()

    async def _upload_file(self, t):
        """上传单文件：>10MB 用并行分块，小文件走自带 upload"""
        if t.size > 10 * 1024 * 1024 and t.parallel > 1:
            return await self._upload_parallel(t)
        peer = await self._resolve_peer(t.dest)
        caption = t.name + (f"\n#{t.tag}" if t.tag else "")
        await self.client.send_file(
            peer, t.path, caption=caption,
            progress_callback=lambda s, tot: self._progress_cb(t, s, tot),
            allow_cache=True,
        )
        return True, ""

    async def _upload_parallel(self, t):
        """并行分块上传：SaveBigFilePart 并发 + InputFileBig 直接发送"""
        peer = await self._resolve_peer(t.dest)
        path = t.path
        size = t.size
        part_count = (size + PART_SIZE - 1) // PART_SIZE
        file_id = random.getrandbits(63)
        sem = asyncio.Semaphore(t.parallel)
        lock = asyncio.Lock()
        sent = {"n": 0}
        start = time.time()

        async def send_part(i):
            with open(path, "rb") as f:
                f.seek(i * PART_SIZE)
                data = f.read(PART_SIZE)
            async with sem:
                await self.client(functions.upload.SaveBigFilePartRequest(
                    file_id=file_id, file_part=i,
                    file_total_parts=part_count, bytes=data
                ))
                async with lock:
                    sent["n"] += len(data)
                    await self._progress_cb(t, sent["n"], size)

        await asyncio.gather(*(send_part(i) for i in range(part_count)))
        t.sent = size
        t.speed = size / max(time.time() - start, 0.01) / 1024 / 1024
        input_file = types.InputFileBig(id=file_id, parts=part_count, name=t.name)
        caption = t.name + (f"\n#{t.tag}" if t.tag else "")
        await self.client.send_file(peer, input_file, caption=caption,
                                    allow_cache=True)
        return True, ""

    # ---------- peer 解析 ----------
    async def _resolve_peer(self, dest):
        """dest: 数字 id 或 @username 或 t.me/xxx"""
        d = str(dest).strip()
        if d.startswith("t.me/"):
            d = d[len("t.me/"):]
        d = d.lstrip("@")
        if d.startswith("-100"):
            return InputPeerChannel(int(d[4:]), 0)
        if d.isdigit():
            return await self.client.get_entity(int(d))
        return await self.client.get_entity(d)

    # ---------- watch 目录 ----------
    async def watch_start(self, directory, interval=30.0, dest="", tag=""):
        if self._watch_task:
            self._watch_task.cancel()
        self._watch_dir = directory
        self._watch_interval = max(5.0, float(interval))
        self._watch_dest = dest
        self._watch_tag = tag
        self._watch_seen = {}
        for root, _, fnames in os.walk(directory):
            for fn in fnames:
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                    self._watch_seen[fp] = (st.st_size, st.st_mtime)
                except OSError:
                    pass
        self._watch_task = asyncio.create_task(self._watch_loop())
        self._notify()
        return True

    def watch_stop(self):
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None
        self._watch_dir = ""
        self._notify()

    async def _watch_loop(self):
        while True:
            try:
                await asyncio.sleep(self._watch_interval)
                new_files = []
                for root, _, fnames in os.walk(self._watch_dir):
                    for fn in fnames:
                        fp = os.path.join(root, fn)
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        key = (st.st_size, st.st_mtime)
                        if fp not in self._watch_seen:
                            self._watch_seen[fp] = key
                            # 文件可能仍在写入：等下一次确认 mtime 稳定再传
                            new_files.append(fp)
                        else:
                            # mtime 变了说明还在写，更新记录等稳定
                            if key != self._watch_seen[fp]:
                                self._watch_seen[fp] = key
                # 只传 mtime 已稳定的新文件（本轮之前已见过且大小不再变化）
                stable = []
                for fp in new_files:
                    try:
                        st = os.stat(fp)
                        if (st.st_size, st.st_mtime) == self._watch_seen.get(fp):
                            stable.append(fp)
                    except OSError:
                        pass
                for fp in stable:
                    t = Task(fp, self._watch_dest, self._watch_tag)
                    self.tasks[t.id] = t
                    await self._queue.put(t)
                    print(f"[watch] 新文件: {fp}")
                if stable:
                    self._notify()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[watch] error: {e}")

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
import json
import os
import random
import time
from collections import OrderedDict

from telethon import TelegramClient
from telethon.tl import types, functions
from telethon.tl.types import InputPeerChannel

PART_SIZE = 512 * 1024  # 512KB，Telegram 官方推荐最大 part
DEFAULT_PARALLEL = 8    # 单文件分块并发数


def _is_permanent_error(err) -> bool:
    """判断错误是否永久性（无需重试）：文件不存在、目标解析失败等。"""
    if not err:
        return False
    s = str(err).lower()
    permanent_markers = (
        "no such file", "not found", "cannot find", "file not found",
        "invalid", "input entity", "username not occupied", "chat_id_invalid",
        "peer_id_invalid", "channel_invalid", "user_not_participant",
        "permission denied", "forbidden", "flood_wait", "file_too_big",
    )
    return any(m in s for m in permanent_markers)


def _mp4_info(path):
    """纯 Python 解析 MP4 moov 元数据：返回 (duration_seconds, width, height)。
    无 ffprobe 依赖；解析失败返回 (0, 0, 0)（Telegram 服务端可自行探测）。
    注意：手机录制的 MP4 moov 常在文件尾部，头部+尾部各读 64MB。"""
    def read_u32(b, off):
        return int.from_bytes(b[off:off+4], "big")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(0)
            head = f.read(min(size, 64 * 1024 * 1024))
            tail = b""
            if size > 64 * 1024 * 1024:
                f.seek(-64 * 1024 * 1024, 2)
                tail = f.read(64 * 1024 * 1024)
    except OSError:
        return 0, 0, 0
    data = head + tail  # 拼接，find_box 会覆盖头尾

    def find_box(data, start, box_type, end=None):
        """按 4 字节类型找子 box（在 data 内搜索）"""
        pos = start
        while pos + 8 <= len(data):
            sz = read_u32(data, pos)
            typ = data[pos+4:pos+8]
            if sz == 1 and pos + 16 <= len(data):
                sz = int.from_bytes(data[pos+8:pos+16], "big")
            if typ == box_type:
                return pos, sz
            if sz < 8:
                break
            pos += sz
        return None

    # 找 moov
    moov = find_box(data, 0, b"moov")
    if not moov:
        return 0, 0, 0
    moov_pos, moov_sz = moov
    # 在 moov 里找 mvhd
    mvhd = find_box(data, moov_pos + 8, b"mvhd", moov_pos + moov_sz)
    duration = 0
    if mvhd:
        p = mvhd[0] + 8  # 跳过 size(4)+type(4) 到 box 内容
        ver = data[p]
        if ver == 1:
            timescale = read_u32(data, p + 20)
            dur = int.from_bytes(data[p+24:p+32], "big")
        else:
            timescale = read_u32(data, p + 12)
            dur = read_u32(data, p + 16)
        if timescale:
            duration = dur / timescale
    # 找第一个 trak/tkhd 拿宽高
    width = height = 0
    trak = find_box(data, moov_pos + 8, b"trak", moov_pos + moov_sz)
    if trak:
        tkhd = find_box(data, trak[0] + 8, b"tkhd", trak[0] + trak[1])
        if tkhd:
            p = tkhd[0] + 8  # 跳过 size(4)+type(4)
            ver = data[p]
            base = p + 20 if ver == 1 else p + 16
            # tkhd: ... width(4) height(4)，16.16 定点
            if base + 8 <= len(data):
                width = read_u32(data, base) >> 16
                height = read_u32(data, base + 4) >> 16
    return duration, width, height


class Task:
    """单个上传任务"""
    STATUS_WAIT = "waiting"
    STATUS_RUN = "uploading"
    STATUS_DONE = "done"
    STATUS_FAIL = "failed"
    STATUS_PAUSED = "paused"
    MAX_RETRIES = 3      # 失败自动重试次数
    RETRY_DELAYS = [5, 15, 30]  # 重试间隔（秒），递增

    def __init__(self, path, dest, tag="", task_id=None):
        self.id = task_id or f"t{int(time.time()*1000)}{random.randint(1000,9999)}"
        self.path = path
        self.name = os.path.basename(path)
        try:
            self.size = os.path.getsize(path)
        except OSError:
            self.size = 0
        self.dest = dest
        self.tag = tag
        self.status = Task.STATUS_WAIT
        self.sent = 0
        self.speed = 0.0
        self.error = ""
        self.elapsed = 0.0
        self.attempt = 0            # 已尝试次数（含重试）
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
            "attempt": self.attempt,
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
        self._watchers = {}                 # wid -> watcher dict
        self._watch_seq = 0
        self._dests = dests or []
        self._state_file = os.environ.get("UPLOAD_STATE_FILE", "/app/data/upload_state.json")
        self._done = {}                     # path -> size（已成功上传，断点续传跳过）
        self._save_dirty = False            # 有变更未落盘
        self._queue_file = os.environ.get("UPLOAD_QUEUE_FILE", "/app/data/upload_queue.json")
        self._pending = []                  # 待传任务 [{path,dest,tag}]，持久化供重启恢复
        self._load_state()
        self._load_queue()
        # 事件回调（路由层注册，进度变化时通知）
        self.on_change = None

    # ---------- 断点续传状态 ----------
    def _load_state(self):
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, "r", encoding="utf-8") as f:
                    self._done = json.load(f) or {}
        except Exception as e:
            print(f"[uploader] 加载断点状态失败: {e}")
            self._done = {}

    def _save_state(self):
        """节流落盘：标记 dirty，由 _flush_state 批量写（避免每个文件全量写 JSON）。"""
        self._save_dirty = True

    def _flush_state(self):
        if not self._save_dirty:
            return
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(self._done, f, ensure_ascii=False)
            self._save_dirty = False
        except Exception as e:
            print(f"[uploader] 保存断点状态失败: {e}")

    def _mark_done(self, path, size):
        self._done[path] = size
        self._save_state()

    # ---------- 待传队列持久化（重启恢复）----------
    def _load_queue(self):
        """加载未完成任务（容器重启后恢复队列，不再丢失）"""
        try:
            if os.path.exists(self._queue_file):
                with open(self._queue_file, "r", encoding="utf-8") as f:
                    self._pending = json.load(f) or []
        except Exception as e:
            print(f"[uploader] 加载待传队列失败: {e}")
            self._pending = []

    def _save_queue(self):
        try:
            os.makedirs(os.path.dirname(self._queue_file), exist_ok=True)
            with open(self._queue_file, "w", encoding="utf-8") as f:
                json.dump(self._pending, f, ensure_ascii=False)
        except Exception as e:
            print(f"[uploader] 保存待传队列失败: {e}")

    def _restore_pending(self):
        """启动时把持久化的待传任务重新入队（跳过已完成的）"""
        restored = 0
        for item in self._pending[:]:
            path = item.get("path", "")
            dest = item.get("dest", "")
            if not path or not dest:
                self._pending.remove(item)
                continue
            try:
                sz = os.path.getsize(path)
            except OSError:
                self._pending.remove(item)
                continue
            if self.is_done(path, sz):
                self._pending.remove(item)  # 已完成，移出待传
                continue
            t = Task(path, dest, item.get("tag", ""))
            self.tasks[t.id] = t
            self._queue.put_nowait(t)
            restored += 1
        if restored:
            print(f"[uploader] 重启恢复 {restored} 个待传任务")
        self._save_queue()
        return restored

    def _drop_pending(self, path):
        """任务结束（成功/永久失败）后从待传队列移除"""
        before = len(self._pending)
        self._pending = [p for p in self._pending if p.get("path") != path]
        if len(self._pending) != before:
            self._save_queue()

    def is_done(self, path, size):
        return self._done.get(path) == size

    def done_count(self):
        return len(self._done)

    # ---------- 状态 ----------
    def _notify(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    async def ensure_workers(self):
        """自愈：运行中但 worker 数不足（或全灭）时补足到 max_concurrent。
        防并发缩容误杀后任务永久卡 waiting（线上事故）。"""
        if not self._running:
            return
        need = self.max_concurrent - len(self._workers)
        if need > 0:
            for _ in range(need):
                self._workers.append(asyncio.create_task(self._worker()))
            print(f"[uploader] 自愈: 补 {need} 个 worker")

    def all_tasks(self):
        return [t.to_json() for t in self.tasks.values()]

    def summary(self):
        st = {}
        for t in self.tasks.values():
            st[t.status] = st.get(t.status, 0) + 1
        return {
            "total": len(self.tasks), "by_status": st,
            "max_concurrent": self.max_concurrent, "paused": self._paused,
            "watch": self.watch_list(),
            "dests": self._dests,
        }

    # ---------- 队列控制 ----------
    async def start(self):
        if self._running:
            return
        self._running = True
        # 容器重启后恢复未完成任务（待传队列持久化）
        self._restore_pending()
        for _ in range(self.max_concurrent):
            w = asyncio.create_task(self._worker())
            self._workers.append(w)

    async def stop(self):
        self._running = False
        for w in self._workers:
            w.cancel()
        self._workers = []
        await self.watch_stop(wid=None)  # 停掉所有 watcher（_watchers 存在才遍历）

    async def add_files(self, paths, dest, tag="", skip_done=True):
        """批量加入任务（paths 可为目录，递归收集）。skip_done=True 跳过已成功上传的（断点续传）。"""
        def collect():
            # 普通同步收集放线程里跑（大目录 os.walk 不阻塞事件循环）
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
            return files
        files = await asyncio.to_thread(collect)  # collect 是同步函数，to_thread 正确
        added = skipped = 0
        # 队列中已有任务路径集合（防重复入队：手动+watch 重叠、重复点上传）
        active_paths = {t.path for t in self.tasks.values()
                        if t.status in (Task.STATUS_WAIT, Task.STATUS_RUN)}
        for f in files:
            try:
                sz = os.path.getsize(f)
            except OSError:
                continue
            if skip_done and self.is_done(f, sz):
                skipped += 1
                continue
            if f in active_paths:
                skipped += 1  # 队列里已有同文件任务（waiting/running），不重复加
                continue
            t = Task(f, dest, tag)
            self.tasks[t.id] = t
            await self._queue.put(t)
            self._pending.append({"path": f, "dest": dest, "tag": tag})
            active_paths.add(f)
            added += 1
        if added:
            self._save_queue()  # 批量入队后统一写盘（避免每个文件一次全量 JSON）
        self._notify()
        return added, skipped

    async def set_concurrent(self, n):
        """动态调整并发数（目标 worker 数）。
        缩容时不 cancel 运行中的 worker（会打断上传并丢任务），
        让多余的 worker 在当前任务完成后自然退出（_worker finally 移除自己）。
        """
        n = max(1, min(int(n), 16))
        self.max_concurrent = n
        await self.ensure_workers()
        self._notify()

    def set_paused(self, paused):
        self._paused = paused
        self._notify()

    async def cancel_task(self, tid):
        t = self.tasks.get(tid)
        if t and t.status in (Task.STATUS_WAIT, Task.STATUS_PAUSED):
            t.status = "cancelled"
            self.tasks.pop(tid, None)
            self._drop_pending(t.path)  # 同时清持久化，防重启后任务复活
            self._notify()
            return True
        return False

    async def cancel_all(self):
        """取消全部上传任务：清空队列 + 取消所有 waiting/paused + 清空待传持久化。
        正在上传的（uploading）继续传完（避免打断分块），取消后整体清空。"""
        cancelled = 0
        # 清空队列里还没被 worker 取走的
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                cancelled += 1
            except asyncio.QueueEmpty:
                break
        # 取消所有 waiting/paused 任务
        for tid in list(self.tasks.keys()):
            t = self.tasks[tid]
            if t.status in (Task.STATUS_WAIT, Task.STATUS_PAUSED):
                t.status = "cancelled"
                self.tasks.pop(tid, None)
                cancelled += 1
        # 清空待传持久化（重启不会恢复这些）
        self._pending = []
        self._save_queue()
        self._notify()
        return cancelled

    # ---------- worker ----------
    async def _worker(self):
        me = asyncio.current_task()
        try:
            while self._running:
                # 缩容后自然退出：worker 数超过 max_concurrent 就不再取新任务
                if len(self._workers) > self.max_concurrent:
                    break
                t = await self._queue.get()
                # 取到任务后二次检查：若自己已多余（并发被调小），放回队列再退出
                if len(self._workers) > self.max_concurrent:
                    await self._queue.put(t)
                    break
                if t.status == "cancelled" or t.id not in self.tasks:
                    self._queue.task_done()
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
                t.attempt += 1
                self._notify()
                try:
                    ok, err = await self._upload_file(t)
                    if ok:
                        t.status = Task.STATUS_DONE
                        self._mark_done(t.path, t.size)
                        self._flush_state()
                        self._drop_pending(t.path)
                    else:
                        # 永久错误（文件丢失/目标无效）不重试，直接 FAIL
                        if _is_permanent_error(err):
                            t.status = Task.STATUS_FAIL
                            t.error = err
                            self._drop_pending(t.path)
                        else:
                            # 自动重试：瞬时错误（网络/代理/超时）重试有效
                            delay = Task.RETRY_DELAYS[t.attempt - 1] if t.attempt <= len(Task.RETRY_DELAYS) else Task.RETRY_DELAYS[-1]
                            if t.attempt < Task.MAX_RETRIES:
                                t.status = Task.STATUS_WAIT
                                t.error = f"第{t.attempt}次失败: {err}（{delay}s 后重试）"
                                self._notify()
                                await asyncio.sleep(delay)
                                await self._queue.put(t)
                                continue
                            t.status = Task.STATUS_FAIL
                            t.error = err
                    t.elapsed = time.time() - t._started_at
                except asyncio.CancelledError:
                    t.status = Task.STATUS_PAUSED
                    self._notify()
                    raise
                except Exception as e:
                    err = str(e)
                    if _is_permanent_error(err):
                        t.status = Task.STATUS_FAIL
                        t.error = err
                    else:
                        delay = Task.RETRY_DELAYS[t.attempt - 1] if t.attempt <= len(Task.RETRY_DELAYS) else Task.RETRY_DELAYS[-1]
                        if t.attempt < Task.MAX_RETRIES:
                            t.status = Task.STATUS_WAIT
                            t.error = f"第{t.attempt}次异常: {err}（{delay}s 后重试）"
                            self._notify()
                            await asyncio.sleep(delay)
                            await self._queue.put(t)
                            continue
                        t.status = Task.STATUS_FAIL
                        t.error = err
                self._notify()
        finally:
            # 关键：退出时把自己从 _workers 移除，否则 len(_workers) 虚高，
            # 导致所有 worker 误判「多余」逐个退出、任务永远卡 waiting（线上事故根因）
            try:
                self._workers.remove(me)
            except ValueError:
                pass

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
            # 读取必须受信号量限制：大文件 part_count 数千，全部先读会撑爆内存/FD
            async with sem:
                with open(path, "rb") as f:
                    f.seek(i * PART_SIZE)
                    data = f.read(PART_SIZE)
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
        # 视频必须带 DocumentAttributeVideo，否则 Telegram 不生成预览封面/时长
        duration, w, h = _mp4_info(path)
        attrs = [types.DocumentAttributeFilename(file_name=t.name)]
        if t.name.lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".webm", ".ts", ".m4v")):
            attrs.insert(0, types.DocumentAttributeVideo(
                duration=int(duration), w=int(w), h=int(h), supports_streaming=True))
        await self.client.send_file(peer, input_file, caption=caption,
                                    attributes=attrs, allow_cache=True)
        return True, ""

    # ---------- peer 解析 ----------
    async def _resolve_peer(self, dest):
        """dest: 数字 id 或 @username 或 t.me/xxx"""
        d = str(dest).strip()
        if d.startswith("t.me/"):
            d = d[len("t.me/"):]
        d = d.lstrip("@")
        # 兼容无 title 频道的显示名 "id:123456"
        if d.startswith("id:"):
            d = d[3:].strip()
        # 数字 id（含 -100 前缀的频道/超群）一律走 get_entity：
        # 不能用 InputPeerChannel(id, 0)——access_hash=0 无法解析，必须走缓存取真实实体
        if d.lstrip("-").isdigit():
            return await self.client.get_entity(int(d))
        return await self.client.get_entity(d)

    # ---------- watch 目录（支持多个）----------
    def watch_list(self):
        """返回 watcher 列表（不含内部 seen 表）"""
        out = []
        for wid, w in self._watchers.items():
            out.append({
                "id": wid,
                "dir": w["dir"],
                "interval": w["interval"],
                "dest": w["dest"],
                "tag": w["tag"],
            })
        return out

    async def watch_start(self, directory, interval=30.0, dest="", tag="", wid=None):
        """新增/更新一个 watcher。wid 为空则新建。"""
        if not wid:
            self._watch_seq += 1
            wid = f"w{self._watch_seq}"
        old = self._watchers.get(wid)
        if old and old.get("task"):
            old["task"].cancel()
        # 初始化 seen：已有文件不再上传（os.walk 移线程，防阻塞）
        seen = {}
        for root, _, fnames in os.walk(directory):
            for fn in fnames:
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                    seen[fp] = (st.st_size, st.st_mtime)
                except OSError:
                    pass
        self._watchers[wid] = {
            "dir": directory,
            "interval": max(5.0, float(interval)),
            "dest": dest,
            "tag": tag,
            "seen": seen,
            "task": asyncio.create_task(self._watch_loop(wid)),
        }
        self._notify()
        return wid

    async def watch_stop(self, wid=None):
        """停止一个（wid）或全部（None） watcher。"""
        if wid is None:
            for w in self._watchers.values():
                if w.get("task"):
                    w["task"].cancel()
            self._watchers.clear()
        else:
            w = self._watchers.pop(wid, None)
            if w and w.get("task"):
                w["task"].cancel()
        self._notify()

    async def _watch_loop(self, wid):
        w = self._watchers.get(wid)
        if not w:
            return
        while wid in self._watchers:
            try:
                await asyncio.sleep(w["interval"])
                new_files = []
                for root, _, fnames in os.walk(w["dir"]):
                    for fn in fnames:
                        fp = os.path.join(root, fn)
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        key = (st.st_size, st.st_mtime)
                        if fp not in w["seen"]:
                            w["seen"][fp] = key
                            # 文件可能仍在写入：等下一次确认 mtime 稳定再传
                            new_files.append(fp)
                        else:
                            # mtime 变了说明还在写，更新记录等稳定
                            if key != w["seen"][fp]:
                                w["seen"][fp] = key
                # 只传 mtime 已稳定的新文件（本轮之前已见过且大小不再变化）
                stable = []
                for fp in new_files:
                    try:
                        st = os.stat(fp)
                        if (st.st_size, st.st_mtime) == w["seen"].get(fp):
                            stable.append(fp)
                    except OSError:
                        pass
                for fp in stable:
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        continue
                    if self.is_done(fp, sz):
                        continue  # 断点续传：已成功过的不再重传
                    if any(t.path == fp and t.status in (Task.STATUS_WAIT, Task.STATUS_RUN)
                           for t in self.tasks.values()):
                        continue  # 队列里已有同文件任务（多 watcher 或手动+watch 重复）
                    t = Task(fp, w["dest"], w["tag"])
                    self.tasks[t.id] = t
                    await self._queue.put(t)
                    self._pending.append({"path": fp, "dest": w["dest"], "tag": w["tag"]})
                    self._save_queue()
                    print(f"[watch:{wid}] 新文件: {fp}")
                if stable:
                    self._notify()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[watch:{wid}] error: {e}")

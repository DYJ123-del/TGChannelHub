#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TGChannelHub bulk uploader — 批量上传本地视频/文件到 Telegram 群组/频道
复用现有登录 session（不重新登录）。

用法:
  python3 bulk_upload.py --dir <目录> --group <chat_id> [--tag 标签] [--resume] [--force-doc]

参数:
  --dir          要遍历的目录（递归）
  --group        目标群组/频道（数字 id 或 @username）
  --tag          给所有消息补加的 #标签（可选，默认用一级子目录名）
  --resume       断点续传：记录已传文件名（<dir>/.uploaded.json），跳过重复
  --force-doc    以"文件"形式发送（不转视频/图片），默认自动类型
  --caption      附加说明文字（放在文件名之后）
"""
import argparse
import asyncio
import json
import os
import sys
import time
import random

# 凭据一律从环境变量读取（TG_API_ID / TG_API_HASH / TG_PROXY_*），不硬编码。
# 代理可选：不设 TG_PROXY_HOST 则直连。

from telethon import TelegramClient, events
from telethon.tl import types, functions
from telethon.tl.types import InputPeerChat, InputPeerChannel


def progress_cb(total, sent, start):
    if total <= 0:
        return
    pct = sent * 100 // total
    el = time.time() - start
    speed = sent / el / 1024 / 1024 if el > 0 else 0
    print(f"\r  进度 {pct:3d}%  {sent/1024/1024:7.1f}/{total/1024/1024:7.1f} MB  {speed:5.1f} MB/s", end="", flush=True)


async def upload_file_parallel(client, path, fname, parallel=8):
    """并行分块上传大文件，返回 InputFileBig（可直接 send_file 免重传）。

    Telegram API 官方推荐：多个并行 call queue 连多个 TCP 连接上传分块。
    Telethon 自带 upload_file 是串行逐块 await，故手动并行。
    """
    size = os.path.getsize(path)
    part_size = 512 * 1024  # 512KB（最大 part，减少 part 数量，官方推荐）
    part_count = (size + part_size - 1) // part_size
    file_id = random.getrandbits(63)

    sem = asyncio.Semaphore(parallel)
    sent = {"n": 0}
    start = time.time()
    lock = asyncio.Lock()

    async def send_part(i):
        with open(path, "rb") as f:
            f.seek(i * part_size)
            data = f.read(part_size)
        async with sem:
            await client(functions.upload.SaveBigFilePartRequest(
                file_id=file_id, file_part=i, file_total_parts=part_count, bytes=data
            ))
            async with lock:
                sent["n"] += len(data)
                progress_cb(size, sent["n"], start)

    # 并发发送全部 parts（可乱序，服务端按序号合并）
    await asyncio.gather(*(send_part(i) for i in range(part_count)))
    print()
    return types.InputFileBig(id=file_id, parts=part_count, name=fname)


async def upload_one(client, peer, path, tag, caption_extra, force_doc, dry=False, parallel=8):
    """上传单个文件，返回 (filename, size, ok, err)"""
    fname = os.path.basename(path)
    size = os.path.getsize(path)
    if size == 0:
        return (fname, size, False, "空文件")
    caption = fname
    if tag:
        caption += f"\n#{tag}"
    if caption_extra:
        caption += f"\n{caption_extra}"
    try:
        if dry:
            await asyncio.sleep(0.001)
        else:
            t0 = time.time()
            # 大文件并行分块上传；小文件走自带 upload
            if size > 10 * 1024 * 1024 and parallel > 1:
                input_file = await upload_file_parallel(client, path, fname, parallel)
                await client.send_file(
                    peer, input_file, caption=caption, force_document=force_doc,
                    progress_callback=lambda s, t: progress_cb(t, s, t0),
                    allow_cache=True,
                )
            else:
                await client.send_file(
                    peer, path, caption=caption, force_document=force_doc,
                    progress_callback=lambda s, t: progress_cb(t, s, t0),
                    allow_cache=True,
                )
            print()
        return (fname, size, True, "")
    except Exception as e:
        return (fname, size, False, str(e))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--group", required=True)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--force-doc", action="store_true")
    ap.add_argument("--caption", default="")
    ap.add_argument("--session", default="/tmp/tg_upload_session")
    ap.add_argument("--limit", type=int, default=0, help="只上传前 N 个文件（测试用）")
    ap.add_argument("--dry", action="store_true", help="只列出要上传的文件，不上传")
    ap.add_argument("--parallel", type=int, default=8, help="并行分块上传并发数（默认 8，仅大文件生效）")
    args = ap.parse_args()

    # 收集文件
    files = []
    for root, dirs, fnames in os.walk(args.dir):
        for f in sorted(fnames):
            p = os.path.join(root, f)
            if os.path.isfile(p) and not p.endswith(".uploaded.json"):
                files.append(p)
    files.sort()
    if args.limit:
        files = files[:args.limit]
    print(f"共 {len(files)} 个文件，总大小 {sum(os.path.getsize(f) for f in files)/1024/1024:.0f} MB")

    # 断点续传记录
    state_file = os.path.join(args.dir, ".uploaded.json")
    done = set()
    if args.resume and os.path.exists(state_file):
        try:
            done = set(json.load(open(state_file)))
            print(f"续传：跳过已上传 {len(done)} 个")
        except Exception:
            done = set()

    if args.dry:
        for f in files:
            print("  [待上传]", os.path.basename(f), f"{os.path.getsize(f)/1024/1024:.0f}MB", "#" + (args.tag or os.path.basename(os.path.dirname(f))))
        return

    proxy = None
    if os.environ.get("TG_PROXY_HOST"):
        proxy = dict(
            proxy_type="http",
            addr=os.environ["TG_PROXY_HOST"],
            port=int(os.environ.get("TG_PROXY_PORT", "7890")),
            rdns=True,
            username=os.environ.get("TG_PROXY_USER") or None,
            password=os.environ.get("TG_PROXY_PASS") or None,
        )

    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        print("❌ 缺少环境变量 TG_API_ID / TG_API_HASH")
        return

    client = TelegramClient(
        args.session,
        int(api_id),
        api_hash,
        proxy=proxy,
    )
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ session 未登录！请先 login（本工具复用现有 session，不应走到这里）")
        return

    me = await client.get_me()
    print(f"✅ 已登录: {me.first_name} ({me.phone})")

    # 解析群组 peer
    g = args.group
    try:
        if str(g).startswith("-100"):
            peer = InputPeerChannel(int(g[4:]), 0)
            await client.get_entity(peer)
        elif g.isdigit():
            peer = await client.get_entity(int(g))
        else:
            peer = await client.get_entity(g)
        print(f"✅ 目标: {getattr(peer, 'title', None) or g} (id={getattr(peer, 'id', None)})")
    except Exception as e:
        print(f"❌ 无法解析群组 {g}: {e}")
        return

    ok = fail = skip = 0
    t_all = time.time()
    for i, f in enumerate(files, 1):
        fname = os.path.basename(f)
        size = os.path.getsize(f)
        if fname in done:
            skip += 1
            continue
        tag = args.tag or os.path.basename(os.path.dirname(f))
        print(f"[{i}/{len(files)}] {fname} ({size/1024/1024:.0f}MB) #tag={tag}", flush=True)
        res = await upload_one(client, peer, f, tag, args.caption, args.force_doc, parallel=args.parallel)
        if res[2]:
            done.add(fname)
            if args.resume:
                json.dump(sorted(done), open(state_file, "w"))
            ok += 1
        else:
            fail += 1
            print(f"  ❌ {fname}: {res[3]}")
        print(f"  累计: 成功{ok} 失败{fail} 跳过{skip}  用时{time.time()-t_all:.0f}s", flush=True)

    print(f"\n🏁 完成: 成功 {ok}, 失败 {fail}, 跳过 {skip}, 总用时 {(time.time()-t_all)/60:.1f} 分钟")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

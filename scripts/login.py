#!/usr/bin/env python3
"""ChannelRelay 登录辅助：send <phone> → sign <code> → password <pw>

用法（环境变量）：
  TG_API_ID / TG_API_HASH / TG_SESSION_NAME 必填
  TG_PROXY_HOST / TG_PROXY_PORT / TG_PROXY_USER / TG_PROXY_PASS 可选（代理）

  python3 scripts/login.py send <手机号>     # 发验证码（TG 官方会话）
  python3 scripts/login.py sign <验证码>     # 验证码登录
  python3 scripts/login.py password <2FA密码> # 两步验证密码
  python3 scripts/login.py verify            # 检查登录状态
"""
import sys, json, os, asyncio

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

proxy = None
if os.environ.get("TG_PROXY_HOST"):
    proxy = {
        "proxy_type": "http",
        "addr": os.environ["TG_PROXY_HOST"],
        "port": int(os.environ.get("TG_PROXY_PORT", "7890")),
        "rdns": True,
    }
    if os.environ.get("TG_PROXY_USER"):
        proxy["username"] = os.environ["TG_PROXY_USER"]
    if os.environ.get("TG_PROXY_PASS"):
        proxy["password"] = os.environ["TG_PROXY_PASS"]

SESSION = os.environ.get("TG_SESSION_NAME", "/app/data/channelrelay")
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STATE = os.path.join(os.path.dirname(SESSION), "login_state.json")

c = TelegramClient(SESSION, API_ID, API_HASH, proxy=proxy)


async def send(phone):
    await c.connect()
    if await c.is_user_authorized():
        me = await c.get_me()
        print(f"ALREADY_AUTHED user={me.first_name} phone={me.phone}")
        return
    sent = await c.send_code_request(phone)
    json.dump({"phone": phone, "phone_code_hash": sent.phone_code_hash}, open(STATE, "w"))
    print(f"CODE_SENT phone={phone}（验证码发到 TG 官方会话，不是短信）")


async def sign(code):
    st = json.load(open(STATE))
    await c.connect()
    try:
        me = await c.sign_in(st["phone"], code, phone_code_hash=st["phone_code_hash"])
        print(f"LOGIN_OK user={me.first_name} phone={me.phone}")
        os.remove(STATE) if os.path.exists(STATE) else None
    except SessionPasswordNeededError:
        print("NEED_2FA_PASSWORD")
    except Exception as e:
        print(f"ERR {type(e).__name__}: {e}")


async def password(pw):
    await c.connect()
    try:
        me = await c.sign_in(password=pw)
        print(f"LOGIN_OK user={me.first_name} phone={me.phone}")
    except Exception as e:
        print(f"ERR {type(e).__name__}: {e}")


async def verify():
    await c.connect()
    print("auth:", await c.is_user_authorized())
    if await c.is_user_authorized():
        me = await c.get_me()
        print("user:", me.first_name, me.phone)


mode = sys.argv[1]
asyncio.run({
    "send": lambda: send(sys.argv[2]),
    "sign": lambda: sign(sys.argv[2]),
    "password": lambda: password(sys.argv[2]),
    "verify": lambda: verify(),
}[mode]())
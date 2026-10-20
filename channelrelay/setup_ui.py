#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TGChannelHub 登录引导页（/setup）

仅在 Telegram session 未认证时生效：
- 所有访问被 middleware 重定向到 /setup
- 引导流程：① 配置说明(api_id/hash 从哪拿) → ② 手机号 → ③ 验证码 → ④ 2FA 密码
- 登录成功后自动放行，/setup 重定向回主页面

安全：引导页仅存在于未登录态；验证码发到 TG 官方会话（手机号本人可收），
send 接口带 5 分钟冷却防刷。
"""
import asyncio
import json
import os
import time

from aiohttp import web
from telethon.errors import SessionPasswordNeededError

from .config import api_id, api_hash
from .log import log

routes = web.RouteTableDef()

# 登录引导状态（内存即可；登录成功由 middleware 放开）
_setup_state = {
    "phone": "",
    "phone_code_hash": None,
    "need_2fa": False,
    "last_send_ts": 0.0,
    "last_attempt_ts": 0.0,   # 任何 send 尝试（含失败）都刷新，防无效请求骚扰
    "sign_fails": 0,          # 连续 sign/password 失败计数
    "lock_until": 0.0,        # 失败过多后的本地锁定截止时间
}
# _authorized 异常冷却：Telethon is_user_authorized 首次走网络，网络抖动一次失败
# 若直接返回 False 会让 middleware 把全站 302 到 /setup（假性锁死）。fail-open：
# 异常时 30s 内放行（各路由自身的密码鉴权兜底），30s 后重试。
_auth_err_until = 0.0

SETUP_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TGChannelHub 登录引导</title>
<style>
:root{--bg:#0e1621;--card:#17212b;--line:#1f2c38;--txt:#e1e3e6;--sub:#708499;--blue:#5eb5f7;--green:#86efac;--red:#f87171}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.box{width:440px;max-width:100%;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:32px 28px}
h1{font-size:20px;margin-bottom:6px;background:linear-gradient(90deg,#60a5fa,#c084fc);-webkit-background-clip:text;-webkit-fill-color:transparent;background-clip:text}
.sub{color:var(--sub);font-size:13px;margin-bottom:22px;line-height:1.6}
.steps{display:flex;gap:6px;margin-bottom:22px}
.step{flex:1;text-align:center;font-size:11px;color:var(--sub);padding:8px 4px;border-radius:8px;background:#0e1621;border:1px solid var(--line)}
.step.cur{color:var(--blue);border-color:var(--blue);font-weight:600}
.step.done{color:var(--green);border-color:var(--green)}
label{display:block;font-size:13px;color:var(--sub);margin:14px 0 6px}
input{width:100%;background:#0e1621;border:1px solid #2a3a49;border-radius:8px;color:var(--txt);padding:11px 12px;font-size:15px;outline:none;box-sizing:border-box}
input:focus{border-color:var(--blue)}
.btn{width:100%;margin-top:18px;padding:12px;background:#2563eb;border:none;border-radius:10px;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
.btn:hover{background:#1d4ed8}
.btn:disabled{opacity:.5;cursor:not-allowed}
.hint{font-size:12px;color:var(--sub);margin-top:10px;line-height:1.7}
.hint a{color:var(--blue)}
.err{color:var(--red);font-size:13px;margin-top:12px;min-height:18px}
.ok{color:var(--green);font-size:13px;margin-top:12px}
.hidden{display:none}
.cfgrow{background:#0e1621;border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-top:8px;font-size:13px}
.cfgrow b{color:var(--txt)}
.cfgrow .val{color:var(--sub);word-break:break-all;font-family:monospace}
</style></head><body>
<div class="box">
  <h1>🔐 TGChannelHub 登录引导</h1>
  <div class="sub">首次部署需要登录你的 Telegram 账号（驱动中转的账号，建议用独立小号）。<br>验证码会发到 <b>Telegram 官方会话</b>（不是短信）。</div>
  <div class="steps">
    <div class="step" id="st1">1 配置</div>
    <div class="step" id="st2">2 手机号</div>
    <div class="step" id="st3">3 验证码</div>
    <div class="step" id="st4">4 完成</div>
  </div>

  <!-- 第 1 步：配置检查 -->
  <div id="cfgPanel">
    <label>Telegram API 配置</label>
    <div class="cfgrow">API ID：<span class="val" id="cfgApiId">…</span></div>
    <div class="cfgrow">API Hash：<span class="val" id="cfgApiHash">…</span></div>
    <div class="hint">
      💡 没有 API ID/Hash？去
      <a href="https://my.telegram.org/apps" target="_blank" rel="noopener">my.telegram.org/apps</a>
      登录后「API development tools」里创建应用即可获得（<b>api_id</b> 数字、<b>api_hash</b> 字符串）。
      <br>如未配置，请在 docker-compose.yml 的 environment 里填 <code>TG_API_ID</code> / <code>TG_API_HASH</code> 后重启容器。
    </div>
    <button class="btn" onclick="cfgOk()">已确认，继续 →</button>
  </div>

  <!-- 第 2 步：手机号 -->
  <div id="phonePanel" class="hidden">
    <label>手机号（含国家区号，如 +8613800138000）</label>
    <input id="phoneInp" placeholder="+86…" autocomplete="tel">
    <button class="btn" id="sendBtn" onclick="sendCode()">发送验证码</button>
    <div class="err" id="err1"></div>
    <div class="hint">验证码将发送到你的 Telegram 账号，而不是手机短信。若收不到，检查代理能否连通 TG。</div>
  </div>

  <!-- 第 3 步：验证码 -->
  <div id="codePanel" class="hidden">
    <label>验证码（TG 官方会话里的数字）</label>
    <input id="codeInp" placeholder="例如 12345" inputmode="numeric" autocomplete="one-time-code">
    <button class="btn" id="signBtn" onclick="signIn()">登录</button>
    <div class="err" id="err2"></div>
    <button class="btn" style="background:transparent;color:var(--sub);border:1px solid var(--line)" onclick="backPhone()">← 换手机号</button>
  </div>

  <!-- 第 4 步：2FA 密码 -->
  <div id="pwPanel" class="hidden">
    <label>两步验证密码（账号开启了 2FA 才需要）</label>
    <input id="pwInp" type="password" placeholder="2FA 密码">
    <button class="btn" id="pwBtn" onclick="submitPw()">完成登录</button>
    <div class="err" id="err3"></div>
  </div>

  <div class="ok hidden" id="okMsg">✅ 登录成功，正在跳转…</div>
</div>
<script>
let need2fa=false;
const $=id=>document.getElementById(id);
function step(n){for(let i=1;i<=4;i++){const e=$('st'+i);e.className='step'+(i===n?' cur':(i<n?' done':''));}}
function show(p){['cfgPanel','phonePanel','codePanel','pwPanel'].forEach(x=>$(x).classList.toggle('hidden',x!==p));}
async function cfgOk(){
  const r=await fetch('/setup/api/status').then(x=>x.json());
  if(r.api_configured){ step(2); show('phonePanel'); $('err1').textContent=''; }
  else{ $('err1').textContent='⚠️ 未检测到 TG_API_ID / TG_API_HASH，请先在 compose 配置后重启容器。'; }
}
async function sendCode(){
  const phone=$('phoneInp').value.trim();
  if(!phone){$('err1').textContent='请输入手机号';return}
  $('sendBtn').disabled=true; $('err1').textContent='发送中…';
  try{
    const r=await fetch('/setup/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})}).then(x=>x.json());
    if(r.ok){ step(3); show('codePanel'); $('err2').textContent=''; $('codeInp').value=''; $('codeInp').focus(); }
    else $('err1').textContent=r.error||'发送失败';
  }finally{ $('sendBtn').disabled=false; }
}
async function signIn(){
  const code=$('codeInp').value.trim();
  if(!code){$('err2').textContent='请输入验证码';return}
  $('signBtn').disabled=true; $('err2').textContent='登录中…';
  try{
    const r=await fetch('/setup/api/sign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})}).then(x=>x.json());
    if(r.ok){ $('okMsg').classList.remove('hidden'); setTimeout(()=>location.href='/',800); }
    else if(r.need_2fa){ need2fa=true; step(4); show('pwPanel'); $('err3').textContent=''; }
    else $('err2').textContent=r.error||'登录失败';
  }finally{ $('signBtn').disabled=false; }
}
async function submitPw(){
  const pw=$('pwInp').value;
  if(!pw){$('err3').textContent='请输入密码';return}
  $('pwBtn').disabled=true; $('err3').textContent='验证中…';
  try{
    const r=await fetch('/setup/api/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})}).then(x=>x.json());
    if(r.ok){ $('okMsg').classList.remove('hidden'); setTimeout(()=>location.href='/',800); }
    else $('err3').textContent=r.error||'密码错误';
  }finally{ $('pwBtn').disabled=false; }
}
function backPhone(){ step(2); show('phonePanel'); _setup_phone=''; }
// 初始化：显示已配置的 API 信息
(async()=>{
  const r=await fetch('/setup/api/status').then(x=>x.json());
  if(r.authenticated){ location.href='/'; return; }   // 已登录不该停在引导页
  $('cfgApiId').textContent=r.api_id || '未配置';
  $('cfgApiHash').textContent=r.api_hash_masked || '未配置';
  if(!r.api_id&&!r.api_configured){ /* 停在配置说明 */ }
})();
</script></body></html>"""


async def _authorized() -> bool:
    global _auth_err_until
    from .telegram import client
    now = time.time()
    # 异常冷却期：放行（各路由自身鉴权兜底），避免网络抖动弹一次把全站锁到 /setup
    if now < _auth_err_until:
        return True
    try:
        return bool(await client.is_user_authorized())
    except Exception:
        _auth_err_until = now + 30
        return True


@routes.get("/setup")
async def setup_page(req: web.Request) -> web.Response:
    if await _authorized():
        return web.Response(status=302, headers={"Location": "/"})
    return web.Response(status=200, content_type="text/html", text=SETUP_HTML)


@routes.get("/setup/api/status")
async def api_status(req: web.Request) -> web.Response:
    au = await _authorized()
    return web.json_response({
        "authenticated": au,
        "api_configured": bool(api_id and api_hash),
        "api_id": str(api_id) if api_id else "",
        # 不返回完整 api_hash：/setup 未登录时公开，hash 属敏感凭证
        "api_hash_masked": (api_hash[:4] + "…" + api_hash[-4:]) if api_hash else "",
        "need_2fa": _setup_state["need_2fa"],
    })


@routes.post("/setup/api/send")
async def api_send(req: web.Request) -> web.Response:
    if await _authorized():
        return web.json_response({"ok": True})
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "请求体须为 JSON"}, status=400)
    phone = (data.get("phone") or "").strip()
    if not phone:
        return web.json_response({"error": "手机号不能为空"}, status=400)
    # 冷却：任何 send 尝试（含失败）5 分钟内只能一次，防刷验证码
    now = time.time()
    if _setup_state["last_attempt_ts"] and now - _setup_state["last_attempt_ts"] < 300:
        remain = int(300 - (now - _setup_state["last_attempt_ts"]))
        return web.json_response({"error": f"发送太频繁，请 {remain} 秒后再试"}, status=429)
    _setup_state["last_attempt_ts"] = now
    from .telegram import client
    try:
        sent = await client.send_code_request(phone)
        _setup_state.update(phone=phone, phone_code_hash=sent.phone_code_hash,
                            need_2fa=False, last_send_ts=now)
        return web.json_response({"ok": True})
    except Exception as e:
        log.warning(f"send_code_request failed: {e}")
        return web.json_response({"error": f"发送失败: {type(e).__name__}"}, status=400)


@routes.post("/setup/api/sign")
async def api_sign(req: web.Request) -> web.Response:
    if await _authorized():
        return web.json_response({"ok": True})
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "请求体须为 JSON"}, status=400)
    code = (data.get("code") or "").strip()
    phone = _setup_state.get("phone") or ""
    phash = _setup_state.get("phone_code_hash")
    if not phone or not phash:
        return web.json_response({"error": "请先发送验证码"}, status=400)
    if not code:
        return web.json_response({"error": "验证码不能为空"}, status=400)
    # 本地限速：15 分钟内连续失败 5 次锁定，防暴力尝试（服务端 FloodWait 兜底之外再加一层）
    now = time.time()
    if now < _setup_state["lock_until"]:
        return web.json_response({"error": "尝试过多，请稍后再试"}, status=429)
    from .telegram import client
    try:
        await client.sign_in(phone, code, phone_code_hash=phash)
        _setup_state.update(need_2fa=False, sign_fails=0)
        return web.json_response({"ok": True})
    except SessionPasswordNeededError:
        _setup_state["need_2fa"] = True
        return web.json_response({"need_2fa": True})
    except Exception as e:
        log.warning(f"sign_in failed: {e}")
        _setup_state["sign_fails"] += 1
        if _setup_state["sign_fails"] >= 5:
            _setup_state["lock_until"] = now + 900
            _setup_state["sign_fails"] = 0
        return web.json_response({"error": f"登录失败: {type(e).__name__}"}, status=400)


@routes.post("/setup/api/password")
async def api_password(req: web.Request) -> web.Response:
    if await _authorized():
        return web.json_response({"ok": True})
    try:
        data = await req.json()
    except Exception:
        return web.json_response({"error": "请求体须为 JSON"}, status=400)
    pw = data.get("password") or ""
    if not pw:
        return web.json_response({"error": "密码不能为空"}, status=400)
    now = time.time()
    if now < _setup_state["lock_until"]:
        return web.json_response({"error": "尝试过多，请稍后再试"}, status=429)
    from .telegram import client
    try:
        await client.sign_in(password=pw)
        _setup_state.update(need_2fa=False, sign_fails=0)
        return web.json_response({"ok": True})
    except Exception as e:
        log.warning(f"2FA sign_in failed: {e}")
        _setup_state["sign_fails"] += 1
        if _setup_state["sign_fails"] >= 5:
            _setup_state["lock_until"] = now + 900
            _setup_state["sign_fails"] = 0
        return web.json_response({"error": f"密码错误: {type(e).__name__}"}, status=400)
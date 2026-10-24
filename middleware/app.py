"""Bridge service entrypoint (FastAPI).

Run: uvicorn app:app --host 0.0.0.0 --port 8090
Env: see .env.example. All secrets live in .env only, never in Git.
"""
import os
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from bridge_core import Bridge, CallStore, ZammadClient, parse_cdr
from export_xlsx import (CALL_COLUMNS, TICKET_COLUMNS, XLSX_MIME, build_xlsx,
                         call_row, ticket_row)

ZAMMAD_URL = os.environ.get("ZAMMAD_URL", "http://zammad-nginx:8080")
ZAMMAD_TOKEN = os.environ.get("ZAMMAD_TOKEN", "")
ZAMMAD_CTI_TOKEN = os.environ.get("ZAMMAD_CTI_TOKEN", "")
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")
RECORDING_DIR = os.environ.get("RECORDING_DIR", "/recordings")
DB_PATH = os.environ.get("BRIDGE_DB", "/data/bridge.db")


def read_recording(rec) -> bytes | None:
    """Recording bytes from the shared recordings volume, or via HTTP URL."""
    if rec.recording_path:
        candidate = rec.recording_path
        if not os.path.isabs(candidate):
            candidate = os.path.join(RECORDING_DIR, candidate)
        # recording_path may be the FreeSWITCH-host absolute path; also try by name
        for path in (candidate, os.path.join(RECORDING_DIR, os.path.basename(candidate))):
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return f.read()
    if rec.recording_url.startswith("http"):
        try:
            with urllib.request.urlopen(rec.recording_url, timeout=20) as r:
                return r.read()
        except Exception:
            return None
    return None


_TICKET_CUSTOMER_ID = os.environ.get("TICKET_CUSTOMER_ID", "")
bridge = Bridge(
    zammad=ZammadClient(
        ZAMMAD_URL, ZAMMAD_TOKEN, cti_token=ZAMMAD_CTI_TOKEN,
        ticket_customer_id=int(_TICKET_CUSTOMER_ID) if _TICKET_CUSTOMER_ID.isdigit() else None),
    store=CallStore(DB_PATH),
    recording_reader=read_recording,
)

app = FastAPI(title="bike-callcenter bridge", version="1.0.0")


def check_secret(x_bridge_secret: str | None):
    if BRIDGE_SECRET and x_bridge_secret != BRIDGE_SECRET:
        raise HTTPException(status_code=401, detail="bad bridge secret")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/events/call")
async def call_event(request: Request, x_bridge_secret: str | None = Header(default=None)):
    check_secret(x_bridge_secret)
    payload = await request.json()
    event = payload.get("event", "newCall")
    return bridge.handle_event(event, payload)


@app.post("/cdr")
async def cdr(request: Request, x_bridge_secret: str | None = Header(default=None)):
    check_secret(x_bridge_secret)
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        payload = await request.json()
    else:  # mod_xml_cdr can post form fields
        form = await request.form()
        payload = dict(form)
    return bridge.handle_cdr(payload)


@app.post("/events/voicemail")
async def voicemail_event(request: Request, x_bridge_secret: str | None = Header(default=None)):
    """Ingest one finished voicemail (posted by the FreeSWITCH hangup hook
    once telephony is live): creates the 语音留言 ticket with recording."""
    check_secret(x_bridge_secret)
    payload = await request.json()
    return bridge.handle_voicemail(payload)


@app.get("/stats/daily")
def daily(date: str):
    return bridge.store.daily_stats(date)


def _xlsx_response(data: bytes, filename: str) -> Response:
    return Response(
        content=data, media_type=XLSX_MIME,
        headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/export/tickets.xlsx")
def export_tickets(days: int = 30, group: str = "", type: str = "",
                   x_bridge_secret: str | None = Header(default=None)):
    """Export the ticket list as .xlsx (mirrors the legacy system's 导出 Excel).

    Query: days=30 (1..365), group=报修组 (optional), type=故障报修 (optional).
    Columns: 工单号/标题/工单类型/服务类型/状态/优先级/分组/客户电话/
    单车编号/服务地点/通话ID/录音地址/投诉等级/创建时间/更新时间.
    """
    check_secret(x_bridge_secret)
    days = max(1, min(days, 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    tickets: list = []
    for page in range(1, 21):  # hard cap: 2000 tickets per export
        batch = bridge.zammad.search_tickets(f"created_at:>{since}",
                                             page=page, per_page=100)
        if not batch:
            break
        tickets.extend(batch)
        if len(batch) < 100:
            break
    state_map = bridge.zammad.id_name_map("/api/v1/ticket_states")
    priority_map = bridge.zammad.id_name_map("/api/v1/ticket_priorities")
    group_map = bridge.zammad.id_name_map("/api/v1/groups")
    phone_map: dict = {}

    def want(t: dict) -> bool:
        if group and group_map.get(t.get("group_id")) != group:
            return False
        if type and str(t.get("ticket_type") or "") != type:
            return False
        return True

    rows = []
    for t in tickets:
        if not want(t):
            continue
        uid = t.get("customer_id")
        if uid and uid not in phone_map:
            phone_map[uid] = bridge.zammad.get_user_phone(uid)
        rows.append(ticket_row(t, state_map, priority_map, group_map, phone_map))
    stamp = datetime.now().strftime("%Y%m%d")
    return _xlsx_response(build_xlsx([("工单列表", TICKET_COLUMNS, rows)]),
                          f"tickets_{stamp}.xlsx")


@app.get("/export/calls.xlsx")
def export_calls(days: int = 30,
                 x_bridge_secret: str | None = Header(default=None)):
    """Export call records (the bridge's SQLite CDR store) as .xlsx."""
    check_secret(x_bridge_secret)
    days = max(1, min(days, 365))
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    rows = [call_row(r) for r in bridge.store.list_calls(start)]
    stamp = datetime.now().strftime("%Y%m%d")
    return _xlsx_response(build_xlsx([("通话记录", CALL_COLUMNS, rows)]),
                          f"calls_{stamp}.xlsx")


TEST_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>模拟来电测试 - 自行车客服系统</title>
<style>
  body { font-family: sans-serif; margin: 24px auto; max-width: 520px; padding: 0 16px; color: #222; }
  h1 { font-size: 20px; }
  label { display: block; margin: 14px 0 6px; font-size: 14px; }
  input, select { width: 100%; padding: 9px; font-size: 15px; box-sizing: border-box; }
  button { margin-top: 20px; width: 100%; padding: 12px; font-size: 16px;
           background: #0e7a5f; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }
  button:disabled { background: #999; }
  #result, #vm_result { margin-top: 18px; padding: 12px; border-radius: 6px; display: none; font-size: 14px; }
  .ok { background: #e6f4ea; border: 1px solid #34a853; }
  .err { background: #fdecea; border: 1px solid #d93025; }
  .hint { color: #666; font-size: 13px; line-height: 1.6; }
</style>
</head>
<body>
<h1>模拟来电测试</h1>
<p class="hint">还没有接电话线路时，用这个页面模拟一通客户来电：系统会像真来电一样
弹出来电提示，接通时工单就自动弹出来（带电话号码），坐席可以在通话过程中
记录；通话结束后录音自动附到这张工单上。来电不带任何预分类：工单先进入客服组，
由坐席在工单里填写服务类型等信息。</p>

<label>客户手机号码</label>
<input id="caller" value="13800000000">

<label>访问密钥（服务器 middleware/.env 里的 BRIDGE_SECRET）</label>
<input id="secret" type="password" placeholder="第一次使用时粘贴一次即可">

<button id="go">发起模拟来电</button>
<div id="result"></div>

<p class="hint">操作顺序：先打开并登录客服系统（端口 8899 的页面）并保持页面开着，
再回到本页点按钮，然后切回客服系统页面看弹屏和新工单。</p>

<hr>
<h1>模拟语音留言</h1>
<p class="hint">模拟客户在无人接听时留了一条语音留言：系统会生成一张「语音留言」工单
（在客服组），留言录音作为附件绑在工单上。坐席在客服系统左侧概览「语音留言」里处理。</p>

<label>留言客户手机号码</label>
<input id="vm_caller" value="13900000000">
<button id="vm_go">模拟留一条语音留言</button>
<div id="vm_result"></div>

<hr>
<h1>导出 Excel</h1>
<p class="hint">把工单列表或通话记录导出成 Excel 表格（.xlsx），和老系统的「导出 Excel」对应：
工单号、客户电话、服务类型、状态、通话ID、录音地址等字段都在里面。</p>

<label>导出范围（最近多少天）</label>
<input id="exp_days" value="30">

<button id="exp_tickets">导出工单列表</button>
<button id="exp_calls" style="background:#1a5fb4">导出通话记录</button>
<div id="exp_result"></div>

<script>
  const params = new URLSearchParams(location.search);
  const saved = params.get("secret") || localStorage.getItem("bridge_secret") || "";
  if (saved) document.getElementById("secret").value = saved;
  document.getElementById("go").addEventListener("click", async () => {
    const btn = document.getElementById("go");
    const box = document.getElementById("result");
    const secret = document.getElementById("secret").value.trim();
    localStorage.setItem("bridge_secret", secret);
    btn.disabled = true; btn.textContent = "正在模拟来电...";
    box.style.display = "none";
    try {
      const resp = await fetch("/simulate/call", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Bridge-Secret": secret},
        body: JSON.stringify({
          caller_id_number: document.getElementById("caller").value.trim()
        })
      });
      const data = await resp.json();
      box.style.display = "block";
      if (resp.ok && data.ticket_id) {
        const link = "http://" + location.hostname + ":8899/#ticket/zoom/" + data.ticket_id;
        box.className = "ok";
        box.innerHTML = "模拟来电完成，工单已生成（编号 " + data.ticket_id +
          "）。<br><a href='" + link + "' target='_blank'>点这里打开这张工单</a>" +
          "<br>录音已作为附件绑在工单上：" + (data.recording_attached ? "是" : "否");
      } else {
        box.className = "err";
        box.textContent = "没有生成工单：" + JSON.stringify(data) +
          "（括号里 zammad_error 就是客服系统返回的具体原因）";
      }
    } catch (e) {
      box.style.display = "block"; box.className = "err";
      box.textContent = "请求失败：" + e;
    }
    btn.disabled = false; btn.textContent = "发起模拟来电";
  });

  document.getElementById("vm_go").addEventListener("click", async () => {
    const btn = document.getElementById("vm_go");
    const box = document.getElementById("vm_result");
    const secret = document.getElementById("secret").value.trim();
    localStorage.setItem("bridge_secret", secret);
    btn.disabled = true; btn.textContent = "正在生成留言...";
    box.style.display = "none";
    try {
      const resp = await fetch("/simulate/voicemail", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Bridge-Secret": secret},
        body: JSON.stringify({
          caller_id_number: document.getElementById("vm_caller").value.trim()
        })
      });
      const data = await resp.json();
      box.style.display = "block";
      if (resp.ok && data.ticket_id) {
        const link = "http://" + location.hostname + ":8899/#ticket/zoom/" + data.ticket_id;
        box.className = "ok";
        box.innerHTML = "语音留言工单已生成（编号 " + data.ticket_id +
          "）。<br><a href='" + link + "' target='_blank'>点这里打开这张留言工单</a>";
      } else {
        box.className = "err";
        box.textContent = "没有生成留言工单：" + JSON.stringify(data);
      }
    } catch (e) {
      box.style.display = "block"; box.className = "err";
      box.textContent = "请求失败：" + e;
    }
    btn.disabled = false; btn.textContent = "模拟留一条语音留言";
  });

  async function downloadXlsx(path, filename) {
    const box = document.getElementById("exp_result");
    const secret = document.getElementById("secret").value.trim();
    localStorage.setItem("bridge_secret", secret);
    const days = document.getElementById("exp_days").value.trim() || "30";
    box.style.display = "none";
    try {
      const resp = await fetch(path + "?days=" + encodeURIComponent(days), {
        headers: {"X-Bridge-Secret": secret}
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const blob = await resp.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      box.style.display = "block"; box.className = "ok";
      box.textContent = "导出成功，文件已开始下载。";
    } catch (e) {
      box.style.display = "block"; box.className = "err";
      box.textContent = "导出失败：" + e + "（常见原因：密钥不对）";
    }
  }
  document.getElementById("exp_tickets").addEventListener("click",
    () => downloadXlsx("/export/tickets.xlsx", "tickets.xlsx"));
  document.getElementById("exp_calls").addEventListener("click",
    () => downloadXlsx("/export/calls.xlsx", "calls.xlsx"));
</script>
</body>
</html>
"""


@app.get("/test", response_class=HTMLResponse)
def test_page():
    """One-click simulated-call page for trials without a phone line (IPPBX)."""
    return TEST_PAGE_HTML


@app.post("/simulate/call")
async def simulate(request: Request, x_bridge_secret: str | None = Header(default=None)):
    """Trial helper: one fake answered call, end to end, no real phone line.

    Creates a 1-second silent WAV in the recordings dir, pushes newCall +
    answer events (the answer opens the ticket immediately, so the agent
    could take notes during the call), then posts a CDR that attaches the
    recording to that same ticket. Safe to run repeatedly (unique call id
    each run).

    The call carries NO pre-classification (real calls arrive plain; the
    agent classifies the ticket in Zammad afterwards), so the ticket lands
    in 客服组 with no 工单类型 stamped. Optional JSON body:
    {"caller_id_number": "138..."} (an "ivr_choice" override is still
    accepted for future IVR tests, but the test page no longer sends one).
    """
    check_secret(x_bridge_secret)
    import struct
    import time
    import uuid as uuidlib
    import wave

    overrides: dict = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            overrides = body
    except Exception:
        pass
    caller = str(overrides.get("caller_id_number") or "13800000000")
    ivr_choice = str(overrides.get("ivr_choice") or "")

    call_id = str(uuidlib.uuid4())
    filename = f"{call_id}.wav"
    path = os.path.join(RECORDING_DIR, filename)
    os.makedirs(RECORDING_DIR, exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(8000)))
    base = {"uuid": call_id, "caller_id_number": caller,
            "destination_number": "4008000000", "direction": "inbound",
            "ivr_choice": ivr_choice, "start_epoch": int(time.time())}
    bridge.handle_event("newCall", base)
    bridge.handle_event("answer", base)
    return bridge.handle_cdr({**base, "billsec": "65", "duration": "75",
                              "recording_path": filename})


@app.post("/simulate/voicemail")
async def simulate_voicemail(request: Request, x_bridge_secret: str | None = Header(default=None)):
    """Trial helper: one fake voicemail, end to end, no real phone line.

    Writes a 3-second silent WAV as the "message", then feeds the
    voicemail ingest path so a 语音留言 ticket with the recording
    attached should appear in 客服组."""
    check_secret(x_bridge_secret)
    import struct
    import time
    import uuid as uuidlib
    import wave

    overrides: dict = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            overrides = body
    except Exception:
        pass
    caller = str(overrides.get("caller_id_number") or "13900000000")

    call_id = str(uuidlib.uuid4())
    filename = f"{call_id}.wav"
    path = os.path.join(RECORDING_DIR, filename)
    os.makedirs(RECORDING_DIR, exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(8000 * 3)))
    return bridge.handle_voicemail({
        "uuid": call_id, "caller_id_number": caller,
        "destination_number": "4008000000", "direction": "inbound",
        "billsec": "8", "duration": "25",
        "start_epoch": int(time.time()), "recording_path": filename})


AGENTS_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>新建客服坐席 - 自行车客服系统</title>
<style>
  body { font-family: sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; }
  label { display: block; margin-top: 16px; font-weight: bold; }
  input { width: 100%; padding: 10px; margin-top: 6px; font-size: 16px; box-sizing: border-box; }
  button { width: 100%; padding: 14px; margin-top: 24px; font-size: 17px; color: #fff;
           background: #1a7a4a; border: 0; border-radius: 8px; cursor: pointer; }
  button:disabled { background: #999; }
  .hint { color: #666; font-size: 13px; line-height: 1.6; }
  #result { display: none; margin-top: 20px; padding: 14px; border-radius: 8px; font-size: 15px; }
  #result.ok { display: block; background: #e6f4ea; }
  #result.err { display: block; background: #fce8e6; }
</style>
</head>
<body>
<h1>新建客服坐席</h1>
<p class="hint">只需要账号和密码：新账号就是客服坐席角色、客服组成员，建好就能在客服系统
（端口 8899 的页面）用这个账号和密码直接登录。姓、名不用分开填，账号就是名字。</p>

<label>账号（用户名）</label>
<input id="login" placeholder="比如：zhangsan">

<label>密码</label>
<input id="password" type="password" placeholder="建议 8 位以上">

<label>确认密码</label>
<input id="confirm" type="password" placeholder="再输一次密码">

<label>访问密钥（服务器 middleware/.env 里的 BRIDGE_SECRET）</label>
<input id="secret" type="password" placeholder="第一次使用时粘贴一次即可">

<button id="go">创建坐席账号</button>
<div id="result"></div>

<script>
  const params = new URLSearchParams(location.search);
  const saved = params.get("secret") || localStorage.getItem("bridge_secret") || "";
  if (saved) document.getElementById("secret").value = saved;
  document.getElementById("go").addEventListener("click", async () => {
    const btn = document.getElementById("go");
    const box = document.getElementById("result");
    const login = document.getElementById("login").value.trim();
    const password = document.getElementById("password").value;
    const confirm = document.getElementById("confirm").value;
    const secret = document.getElementById("secret").value.trim();
    localStorage.setItem("bridge_secret", secret);
    if (!login || !password) {
      box.style.display = "block"; box.className = "err";
      box.textContent = "账号和密码都要填。";
      return;
    }
    if (password !== confirm) {
      box.style.display = "block"; box.className = "err";
      box.textContent = "两次密码不一致，请重新输入。";
      return;
    }
    btn.disabled = true; btn.textContent = "正在创建...";
    box.style.display = "none";
    try {
      const resp = await fetch("/agents", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-Bridge-Secret": secret},
        body: JSON.stringify({login: login, password: password})
      });
      const data = await resp.json();
      box.style.display = "block";
      if (resp.ok && data.ok) {
        box.className = "ok";
        box.textContent = "已创建客服坐席：" + data.login +
          "，请用这个账号和密码登录客服系统（端口 8899）。";
      } else {
        box.className = "err";
        box.textContent = "创建失败：" +
          (data.error === "exists" ? "这个账号已经存在，换一个账号再试。"
                                   : (data.error || JSON.stringify(data)));
      }
    } catch (e) {
      box.style.display = "block"; box.className = "err";
      box.textContent = "请求失败：" + e;
    }
    btn.disabled = false; btn.textContent = "创建坐席账号";
  });
</script>
</body></html>
"""


@app.get("/agents", response_class=HTMLResponse)
def agents_page():
    """Simple agent-account creation page: login + password only."""
    return AGENTS_PAGE_HTML


@app.post("/agents")
async def create_agent_account(request: Request, x_bridge_secret: str | None = Header(default=None)):
    """Create an agent account from just a login + password (secret-protected)."""
    check_secret(x_bridge_secret)
    try:
        body = await request.json()
    except Exception:
        body = {}
    login = str((body or {}).get("login") or "").strip()
    password = str((body or {}).get("password") or "")
    if not login or not password:
        return {"ok": False, "error": "login and password are required"}
    user_id, err = bridge.zammad.create_agent(login, password)
    if user_id is None:
        return {"ok": False, "error": "账号已存在" if err == "exists" else err}
    return {"ok": True, "login": login, "user_id": user_id}


# --- agent board: a ticket pops open by itself, not a list badge --------
# Why this page exists: Zammad itself never opens a ticket on its own -
# a new ticket only shows up as a dot on an overview list, and the agent
# has to notice the dot and click through. During a live call that is
# too slow. So agents keep this board open: it polls the bridge, and the
# moment a new ticket is created it navigates THIS tab straight to the
# Zammad ticket (self-navigation is not blocked by popup blockers).
# After finishing the ticket, the agent returns to the board bookmark;
# the next call then pops open the same way.
BOARD_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>坐席来电看板 - 自行车客服系统</title>
<style>
  body { font-family: sans-serif; margin: 24px auto; max-width: 560px; padding: 0 16px; color: #222; }
  h1 { font-size: 20px; }
  label { display: block; margin: 14px 0 6px; font-size: 14px; }
  input { width: 100%; padding: 9px; font-size: 15px; box-sizing: border-box; }
  button { margin-top: 20px; width: 100%; padding: 12px; font-size: 16px;
           background: #0e7a5f; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }
  button:disabled { background: #999; }
  .hint { color: #666; font-size: 13px; line-height: 1.6; }
  #status { margin-top: 16px; padding: 10px; border-radius: 6px; background: #f1f1f1; font-size: 14px; }
  #alert { display: none; margin-top: 16px; padding: 20px; border-radius: 8px;
           background: #d93025; color: #fff; font-size: 22px; text-align: center;
           animation: flash 0.8s infinite alternate; }
  @keyframes flash { from { opacity: 1; } to { opacity: 0.55; } }
  #recent { margin-top: 16px; font-size: 14px; line-height: 1.9; }
  #recent a { color: #0e7a5f; }
</style>
</head>
<body>
<h1>坐席来电看板</h1>
<p class="hint">上班时打开这个页面并点“开始值班”，来电时页面会自动跳到新工单，不用盯着待办列表的小点。
工单打开后这个标签页就变成工单页了，处理完请点收藏夹回到看板，下一通电话继续自动弹。</p>

<label>访问密钥（服务器 middleware/.env 里的 BRIDGE_SECRET）</label>
<input id="secret" type="password" placeholder="第一次使用时粘贴一次即可">

<button id="go">开始值班</button>
<div id="status">还没有开始值班。</div>
<div id="alert"></div>
<div id="recent"></div>

<script>
let baseline = 0, watching = false, timer = null, audioCtx = null;
const params = new URLSearchParams(location.search);
const saved = params.get("secret") || localStorage.getItem("bridge_secret") || "";
if (saved) document.getElementById("secret").value = saved;

function zammadTicketUrl(id) {
  return location.protocol + "//" + location.hostname + ":8899/#ticket/zoom/" + id;
}
function beep() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    o.frequency.value = 880; g.gain.value = 0.15;
    o.start(); setTimeout(() => o.stop(), 600);
  } catch (e) {}
}
async function poll() {
  const secret = document.getElementById("secret").value.trim();
  let resp;
  try {
    resp = await fetch("/board/feed", { headers: { "X-Bridge-Secret": secret } });
  } catch (e) { return; }
  if (resp.status === 401) {
    document.getElementById("status").textContent = "密钥不对，请重新粘贴访问密钥。";
    return;
  }
  if (!resp.ok) return;
  const data = await resp.json();
  const rows = data.tickets || [];
  document.getElementById("recent").innerHTML = rows.map(r =>
    "<div>" + r.created_at + " 来电 " + r.caller +
    " <a target='_blank' href='" + zammadTicketUrl(r.ticket_id) + "'>工单 #" + r.ticket_id + "</a></div>"
  ).join("") || "暂无工单。";
  if (!rows.length) return;
  const newest = rows[0];
  if (newest.rowid > baseline) {
    baseline = newest.rowid;
    const box = document.getElementById("alert");
    box.style.display = "block";
    box.textContent = "新来电 " + newest.caller + "，正在打开工单 #" + newest.ticket_id + "…";
    document.getElementById("status").textContent = "新来电，正在打开工单…";
    beep();
    setTimeout(() => { location.href = zammadTicketUrl(newest.ticket_id); }, 2000);
  }
}
document.getElementById("go").onclick = async () => {
  const secret = document.getElementById("secret").value.trim();
  localStorage.setItem("bridge_secret", secret);
  try { if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {}
  document.getElementById("status").textContent = "值班中，正在等来电…";
  document.getElementById("go").disabled = true;
  const resp = await fetch("/board/feed", { headers: { "X-Bridge-Secret": secret } });
  if (resp.ok) {
    const data = await resp.json();
    const rows = data.tickets || [];
    baseline = rows.length ? rows[0].rowid : 0;
  }
  watching = true;
  timer = setInterval(poll, 2000);
  poll();
};
</script>
</body>
</html>
"""


@app.get("/board", response_class=HTMLResponse)
def board_page():
    """Agent board: keep open on shift; new tickets open themselves."""
    return BOARD_PAGE_HTML


@app.get("/board/feed")
def board_feed(x_bridge_secret: str | None = Header(default=None)):
    """Newest ticketed calls, newest first (secret-protected)."""
    check_secret(x_bridge_secret)
    return {"tickets": bridge.store.recent_tickets(20)}

"""Record service entrypoint (FastAPI).

Run: uvicorn app:app --host 0.0.0.0 --port 8090
Env: see .env.example. All secrets live in .env only, never in Git.

This is a RECORD system, not a ticket system: no Zammad, no workflow.
Pages: / (login + desk: ONE system for agents), /login, /setup
(admin seat passwords), /test (simulated calls without a line).
The desk, after login, holds call pop-ups, the record list, record
detail + notes + audio, the busy toggle, and per-seat stats/history.
Old /board and /records URLs redirect to /desk.
"""
import html
import os
import threading
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from bridge_core import Bridge, CallStore, summarize_call_history
from export_xlsx import CALL_COLUMNS, XLSX_MIME, build_xlsx, call_row

BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")
RECORDING_DIR = os.environ.get("RECORDING_DIR", "/recordings")
DB_PATH = os.environ.get("BRIDGE_DB", "/data/bridge.db")


def find_recording_file(stored_path: str) -> str | None:
    """Resolve a stored recording path to a file on disk, or None.
    Stored paths may be the FreeSWITCH-host absolute path or just a
    filename; both are tried against the shared recordings volume."""
    if not stored_path:
        return None
    candidates = [stored_path]
    if not os.path.isabs(stored_path):
        candidates.append(os.path.join(RECORDING_DIR, stored_path))
    candidates.append(os.path.join(RECORDING_DIR, os.path.basename(stored_path)))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# Seat numbers in roster order for voicemail dispatch (fewest-calls).
# These are the FreeSWITCH extensions, so queue CDRs (cc_agent) line up.
_ROTATION_AGENTS = [a.strip() for a in
                    os.environ.get("ROTATION_AGENTS", "8001,8002,8003").split(",")
                    if a.strip()]
bridge = Bridge(
    store=CallStore(DB_PATH),
    rotation_agents=_ROTATION_AGENTS,
)

# ------------------------------------------------------------------ login
# One login, one system: a seat account (8001/8002/8003) logs in at /
# and lands on the desk. Login = Available by default (calls are sent
# to the seat); only a self-set On Break stops distribution. Sessions
# are random tokens in an HttpOnly cookie; the desk page touches its
# session on every poll, so closing the browser without logout lets
# the seat go stale and the cleanup thread below logs it out of the
# queue (10 minutes of silence).
SESSION_COOKIE = "bike_session"


def get_seat(request: Request) -> str | None:
    """Seat for the request's session cookie, or None. Validating
    touches the heartbeat, so an open desk page keeps the seat live."""
    return bridge.store.get_session_seat(request.cookies.get(SESSION_COOKIE))


def require_seat(request: Request) -> str:
    seat = get_seat(request)
    if not seat:
        raise HTTPException(status_code=401, detail="请先登录")
    return seat


def _cleanup_loop() -> None:
    """Background: log out seats whose browsers went silent."""
    import time as _time

    while True:
        try:
            bridge.logout_stale_seats()
        except Exception:
            pass
        _time.sleep(60)


try:
    threading.Thread(target=_cleanup_loop, daemon=True).start()
except Exception:
    pass

app = FastAPI(title="bike-callcenter bridge", version="1.0.0")


def check_secret(x_bridge_secret: str | None, alt_secret: str | None = None):
    # FreeSWITCH's json_cdr cannot send custom headers, so the CDR
    # endpoint also accepts ?secret= (localhost traffic only, same host
    # as the .env file). All other endpoints are header-only.
    if BRIDGE_SECRET and x_bridge_secret != BRIDGE_SECRET and alt_secret != BRIDGE_SECRET:
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
    check_secret(x_bridge_secret, request.query_params.get("secret"))
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
    once telephony is live): creates the 语音留言 record with recording."""
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


@app.get("/export/calls.xlsx")
def export_calls(days: int = 30,
                 x_bridge_secret: str | None = Header(default=None)):
    """Export call records (the bridge's SQLite record store) as .xlsx.
    Columns mirror the legacy 通话记录 list: time, caller, agent,
    duration, channel, service type, question notes, recording file, CallID."""
    check_secret(x_bridge_secret)
    days = max(1, min(days, 365))
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    rows = [call_row(r) for r in bridge.store.list_calls(start)]
    stamp = datetime.now().strftime("%Y%m%d")
    return _xlsx_response(build_xlsx([("通话记录", CALL_COLUMNS, rows)]),
                          f"records_{stamp}.xlsx")


TEST_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>模拟来电测试 - 自行车客服系统</title>
<style>
  body { font-family: -apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
         margin: 0; padding: 24px 16px; color: #1c1c1e; background: #f5f5f7; -webkit-font-smoothing: antialiased; }
  body::before { content: ""; position: fixed; inset: 0; z-index: 0; pointer-events: none;
    background:
      radial-gradient(600px 400px at 12% -5%, rgba(0,122,255,.12), transparent 60%),
      radial-gradient(520px 420px at 95% 5%, rgba(88,86,214,.10), transparent 60%),
      radial-gradient(720px 520px at 50% 115%, rgba(52,199,123,.10), transparent 60%); }
  .card { max-width: 560px; margin: 0 auto; position: relative; z-index: 1;
    background: rgba(255,255,255,.72); backdrop-filter: blur(20px) saturate(180%);
    -webkit-backdrop-filter: blur(20px) saturate(180%);
    border-radius: 24px; box-shadow: 0 8px 32px rgba(0,0,0,.08);
    border: 1px solid rgba(255,255,255,.65); padding: 24px 26px; }
  h1 { font-size: 22px; letter-spacing: -.4px; }
  label { display: block; margin: 14px 0 6px; font-size: 14px; color: #8a8a8e; }
  input, select { width: 100%; padding: 10px 13px; font-size: 15px; box-sizing: border-box;
    border: 1px solid rgba(0,0,0,.08); border-radius: 12px; background: rgba(255,255,255,.85); }
  input:focus, select:focus { outline: none; border-color: #007aff; box-shadow: 0 0 0 4px rgba(0,122,255,.15); }
  button { margin-top: 20px; width: 100%; padding: 12px; font-size: 16px; font-weight: 600;
           background: #007aff; color: #fff; border: 0; border-radius: 999px; cursor: pointer;
           box-shadow: 0 2px 10px rgba(0,122,255,.30); transition: transform .15s; }
  button:active { transform: scale(.97); }
  button:disabled { background: #999; box-shadow: none; }
  #result, #vm_result { margin-top: 18px; padding: 12px 14px; border-radius: 14px; display: none; font-size: 14px; }
  .ok { background: rgba(52,199,123,.14); color: #248a3d; }
  .err { background: rgba(255,59,48,.12); color: #d70015; }
  .hint { color: #8a8a8e; font-size: 13px; line-height: 1.6; }
  hr { border: 0; border-top: 1px solid rgba(0,0,0,.06); margin: 24px 0; }
  a { color: #007aff; }
</style>
</head>
<body>
<div class="card">
<h1>模拟来电测试</h1>
<p class="hint">还没有接电话线路时，用这个页面模拟一通客户来电：系统会像真来电一样
弹出来电提示，接通时记录就自动弹出来（带电话号码），坐席可以在通话过程中
记录问题；通话结束后录音自动绑到这条记录上。来电不带任何预分类：记录先
进入记录列表，由坐席在记录里填写服务类型等信息。</p>

<label>客户手机号码</label>
<input id="caller" value="13800000000">

<label>访问密钥（服务器 middleware/.env 里的 BRIDGE_SECRET）</label>
<input id="secret" type="password" placeholder="第一次使用时粘贴一次即可">

<button id="go">发起模拟来电</button>
<div id="result"></div>

<p class="hint">操作顺序：先用坐席账号登录工作台（/），登录后即自动在线接单，
再回到本页点按钮，然后切回工作台看弹屏和新记录。</p>

<hr>
<h1>模拟语音留言</h1>
<p class="hint">模拟客户在无人接听时留了一条语音留言：系统会生成一条「语音留言」记录，
留言录音绑在这条记录上。坐席在记录列表里按类型筛选留言处理。</p>

<label>留言客户手机号码</label>
<input id="vm_caller" value="13900000000">
<button id="vm_go">模拟留一条语音留言</button>
<div id="vm_result"></div>

<hr>
<h1>导出 Excel</h1>
<p class="hint">把通话记录导出成 Excel 表格（.xlsx），和老系统的「导出 Excel」对应：
登记时间、联系电话、客服人员、通话时长、服务途径、服务类型、通话内容、
录音文件、通话ID等字段都在里面。</p>

<label>导出范围（最近多少天）</label>
<input id="exp_days" value="30">

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
      if (resp.ok && data.record_id) {
        const link = "/desk?record=" + data.record_id;
        box.className = "ok";
        box.innerHTML = "模拟来电完成，记录已生成（编号 " + data.record_id +
          "）。<br><a href='" + link + "' target='_blank'>点这里打开这条记录</a>" +
          "<br>录音已绑在这条记录上：" + (data.has_recording ? "是" : "否");
      } else {
        box.className = "err";
        box.textContent = "没有生成记录：" + JSON.stringify(data);
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
      if (resp.ok && data.record_id) {
        const link = "/desk?record=" + data.record_id;
        box.className = "ok";
        box.innerHTML = "语音留言记录已生成（编号 " + data.record_id +
          "）。<br><a href='" + link + "' target='_blank'>点这里打开这条留言记录</a>";
      } else {
        box.className = "err";
        box.textContent = "没有生成留言记录：" + JSON.stringify(data);
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
  document.getElementById("exp_calls").addEventListener("click",
    () => downloadXlsx("/export/calls.xlsx", "records.xlsx"));
</script>
</div>
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
    answer events (the answer opens the record immediately, so the agent
    could take notes during the call), then posts a CDR that binds the
    recording to that same record. Safe to run repeatedly (unique call id
    each run).

    The call carries NO pre-classification (real calls arrive plain; the
    agent fills in the service type on the record afterwards), so the
    record carries no 服务类型 yet. Optional JSON body:
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
            "ivr_choice": ivr_choice, "start_epoch": int(time.time()),
            "bridge_ticket": "1"}
    bridge.handle_event("newCall", base)
    bridge.handle_event("answer", base)
    return bridge.handle_cdr({**base, "billsec": "65", "duration": "75",
                              "recording_path": filename})


@app.post("/simulate/voicemail")
async def simulate_voicemail(request: Request, x_bridge_secret: str | None = Header(default=None)):
    """Trial helper: one fake voicemail, end to end, no real phone line.

    Writes a 3-second silent WAV as the "message", then feeds the
    voicemail ingest path so a 语音留言 record with the recording
    bound should appear in the record list."""
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




# ------------------------------------------------------------------ desk
# One login, one system: /login -> /desk. The desk holds call pop-ups,
# the record list, record detail + notes + audio, the busy toggle, and
# per-seat stats/history. Old /board and /records redirect here.

BASE_CSS = """
:root{--blue:#007aff;--blue-dk:#0062cc;--bg:#f5f5f7;--text:#1c1c1e;--muted:#8a8a8e;
--line:rgba(0,0,0,.06);--red:#ff3b30;--red-bg:rgba(255,59,48,.12);--amber:#ff9500;
--amber-bg:rgba(255,149,0,.14);--green:#34c759;--green-bg:rgba(52,199,123,.14);
--gray-bg:rgba(120,120,128,.12)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans SC",sans-serif;
-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
background:
radial-gradient(600px 400px at 12% -5%, rgba(0,122,255,.12), transparent 60%),
radial-gradient(520px 420px at 95% 5%, rgba(88,86,214,.10), transparent 60%),
radial-gradient(720px 520px at 50% 115%, rgba(52,199,123,.10), transparent 60%)}
.topbar,.wrap,.center{position:relative;z-index:1}
a{color:var(--blue)}
.topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:14px;
padding:12px 20px;flex-wrap:wrap;
background:rgba(255,255,255,.72);backdrop-filter:blur(20px) saturate(180%);
-webkit-backdrop-filter:blur(20px) saturate(180%);
border-bottom:1px solid rgba(0,0,0,.06)}
.topbar .brand{font-size:19px;font-weight:700;letter-spacing:-.3px}
.topbar .spacer{flex:1}
.pill{display:inline-block;padding:4px 13px;border-radius:999px;font-size:13px;font-weight:600;white-space:nowrap}
.pill-green{background:var(--green-bg);color:#248a3d}
.pill-amber{background:var(--amber-bg);color:#a15c00}
.pill-gray{background:var(--gray-bg);color:#6b7280}
.pill-red{background:var(--red-bg);color:#d70015}
.btn{border:0;border-radius:999px;padding:10px 20px;font-size:15px;font-weight:600;cursor:pointer;
background:var(--blue);color:#fff;box-shadow:0 2px 10px rgba(0,122,255,.30);
transition:transform .15s,background .15s}
.btn:hover{background:var(--blue-dk)}
.btn:active{transform:scale(.97)}
.btn-ghost{background:rgba(255,255,255,.65);color:var(--text);border:1px solid rgba(0,0,0,.08);
box-shadow:none;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
.btn-ghost:hover{background:rgba(255,255,255,.9)}
.btn-sm{padding:7px 14px;font-size:13px}
input,select,textarea{border:1px solid rgba(0,0,0,.08);border-radius:12px;padding:10px 13px;
font-size:15px;width:100%;background:rgba(255,255,255,.85)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--blue);
box-shadow:0 0 0 4px rgba(0,122,255,.15)}
label{font-size:14px;color:var(--muted);display:block;margin:12px 0 4px}
.wrap{max-width:1180px;margin:0 auto;padding:20px}
.card{background:rgba(255,255,255,.72);border-radius:24px;
box-shadow:0 8px 32px rgba(0,0,0,.08);border:1px solid rgba(255,255,255,.65);
backdrop-filter:blur(20px) saturate(180%);-webkit-backdrop-filter:blur(20px) saturate(180%);
padding:20px 22px}
.card h2{margin:0 0 12px;font-size:17px;font-weight:700;letter-spacing:-.2px}
.grid{display:grid;grid-template-columns:1.55fr 1fr;gap:20px;align-items:start}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.center{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.auth-card{width:400px;max-width:100%}
.auth-card h1{margin:0 0 4px;font-size:24px;font-weight:700;letter-spacing:-.4px}
.auth-card .sub{color:var(--muted);font-size:14px;margin:0 0 8px;line-height:1.6}
.msg{border-radius:14px;padding:11px 15px;font-size:14px;margin:12px 0;line-height:1.6}
.msg-err{background:var(--red-bg);color:#d70015}
.msg-ok{background:var(--green-bg);color:#248a3d}
.msg-info{background:rgba(0,122,255,.10);color:#0057b8}
.foot{font-size:13px;color:var(--muted);text-align:center;margin-top:16px}
"""

DESK_CSS = """
.alert{display:none;border-radius:24px;padding:16px 22px;margin-bottom:20px;
background:rgba(255,59,48,.88);color:#fff;backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
box-shadow:0 8px 28px rgba(255,59,48,.35)}
.alert.show{display:block;animation:flash 1.2s infinite}
@keyframes flash{0%,100%{box-shadow:0 8px 28px rgba(255,59,48,.35)}50%{box-shadow:0 8px 36px rgba(255,59,48,.55)}}
.alert b{font-size:22px}
.alert small{font-size:14px;opacity:.9}
.search{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.search input,.search select{width:auto;flex:1;min-width:110px}
.seg{display:flex;background:var(--gray-bg);border-radius:10px;padding:2px;gap:2px}
.seg button{border:0;background:transparent;padding:7px 14px;border-radius:8px;font-size:14px;
cursor:pointer;color:var(--text);box-shadow:none}
.seg button.on{background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.15);font-weight:600}
.switch-wrap{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:500}
.switch{position:relative;display:inline-block;width:51px;height:31px;flex:none}
.switch input{display:none}
.switch .slider{position:absolute;inset:0;background:rgba(120,120,128,.32);border-radius:999px;
transition:.2s;cursor:pointer}
.switch .slider:before{content:"";position:absolute;width:27px;height:27px;left:2px;top:2px;
background:#fff;border-radius:50%;box-shadow:0 2px 6px rgba(0,0,0,.25);transition:.2s}
.switch input:checked + .slider{background:#34c759}
.switch input:checked + .slider:before{transform:translateX(20px)}
.row{display:grid;grid-template-columns:140px 64px 1fr 64px 110px;gap:8px;padding:11px 8px;
border-bottom:1px solid var(--line);cursor:pointer;font-size:14px;align-items:center}
.row:hover{background:rgba(0,0,0,.02)}
.row:active{background:rgba(0,0,0,.04)}
.row .caller{font-weight:600}
.row .q{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.seats{display:flex;gap:10px;flex-wrap:wrap}
.seat-chip{border:1px solid rgba(255,255,255,.8);background:rgba(255,255,255,.55);
border-radius:999px;padding:6px 14px;font-size:14px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.dot-green{background:#34c759}.dot-amber{background:#ff9500}.dot-gray{background:#8a8a8e}
.stats{display:flex;gap:28px;margin-bottom:8px}
.stat b{font-size:28px;display:block;letter-spacing:-.5px}
.stat span{color:var(--muted);font-size:13px}
.hist{font-size:14px;padding:8px 0;border-bottom:1px solid var(--line)}
.hist .t{color:var(--muted);font-size:13px}
dl{margin:0}
dt{color:var(--muted);font-size:13px;margin-top:10px}
dd{margin:2px 0 0;font-size:15px}
audio{width:100%;margin-top:8px}
.pager{display:flex;gap:8px;justify-content:space-between;margin-top:14px}
.empty{color:var(--muted);text-align:center;padding:30px 0}
.detail-sticky{position:sticky;top:68px}
@media(max-width:900px){.detail-sticky{position:static}}
"""


def _page(title: str, body: str, extra_css: str = "") -> str:
    return ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>" + title + "</title><style>" + BASE_CSS + extra_css +
            "</style></head><body>" + body + "</body></html>")


def login_page(seats: list, has_agents: bool, error: str = "") -> str:
    options = "".join(
        "<option value=\"" + html.escape(s) + "\">" + html.escape(s) + "</option>"
        for s in seats)
    err = ("<div class=\"msg msg-err\">" + html.escape(error) + "</div>") if error else ""
    notice = ""
    if not has_agents:
        notice = ("<div class=\"msg msg-info\">还没有设置坐席密码，"
                  "请管理员先打开 <a href=\"/setup\">坐席密码设置</a> 设置好再登录。</div>")
    body = ("<div class=\"center\"><div class=\"card auth-card\">"
            "<h1>🚲 自行车客服工作台</h1>"
            "<p class=\"sub\">请用坐席账号登录。登录后即自动在线接单，"
            "只有自己置忙时才不分单。</p>"
            + err + notice +
            "<form method=\"post\" action=\"/login\">"
            "<label>坐席账号</label><select name=\"seat\">" + options + "</select>"
            "<label>登录密码</label>"
            "<input type=\"password\" name=\"password\" required autofocus>"
            "<button class=\"btn\" style=\"width:100%;margin-top:18px\">登录</button>"
            "</form>"
            "<p class=\"foot\"><a href=\"/setup\">管理员设置坐席密码</a></p>"
            "</div></div>")
    return _page("登录 - 自行车客服工作台", body)


def setup_page(seats: list, error: str = "", ok: str = "") -> str:
    err = ("<div class=\"msg msg-err\">" + html.escape(error) + "</div>") if error else ""
    good = ("<div class=\"msg msg-ok\">" + html.escape(ok) + "</div>") if ok else ""
    fields = ""
    for s in seats:
        fields += ("<label>坐席 " + html.escape(s) + " 新密码</label>"
                   "<input type=\"password\" name=\"pw_" + html.escape(s) + "\">"
                   "<label>坐席 " + html.escape(s) + " 再输一次</label>"
                   "<input type=\"password\" name=\"pw2_" + html.escape(s) + "\">")
    body = ("<div class=\"center\"><div class=\"card auth-card\" style=\"width:480px\">"
            "<h1>坐席密码设置</h1>"
            "<p class=\"sub\">管理员专用：需要桥接密钥（服务器 middleware/.env 里的 "
            "BRIDGE_SECRET）。只填要设置或修改的坐席，留空表示不动。</p>"
            + err + good +
            "<form method=\"post\" action=\"/setup\">"
            "<label>桥接密钥</label><input type=\"password\" name=\"secret\" required>"
            + fields +
            "<button class=\"btn\" style=\"width:100%;margin-top:18px\">保存</button>"
            "</form>"
            "<p class=\"foot\"><a href=\"/login\">返回登录</a></p>"
            "</div></div>")
    return _page("坐席密码设置 - 自行车客服工作台", body)


SERVICE_TYPES = ["还车异常", "借车异常", "锁桩故障", "车辆损坏",
                 "掉链", "乱停放", "扣费退款", "其他"]


def desk_page_html(seat: str) -> str:
    body = """
<div class="topbar">
  <div class="brand">🚲 自行车客服工作台</div>
  <div class="spacer"></div>
  <span>坐席 <b id="seatLabel">__SEAT__</b></span>
  <span id="statusPill" class="pill pill-gray">未知</span>
  <label class="switch-wrap"><span>接单</span><span class="switch"><input type="checkbox" id="busySwitch" checked><span class="slider"></span></span></label>
  <form method="post" action="/logout" style="margin:0">
    <button class="btn btn-sm btn-ghost">退出</button>
  </form>
</div>
<div class="wrap">
  <div id="alert" class="alert">
    <span id="alertText"></span>
    <button class="btn btn-sm btn-ghost" style="float:right"
      onclick="document.getElementById('alert').classList.remove('show')">知道了</button>
  </div>
  <div class="card" style="margin-bottom:20px">
    <h2>坐席状态</h2>
    <div id="seatStrip" class="seats"><span style="color:var(--muted)">加载中…</span></div>
  </div>
  <div class="grid">
    <div class="card">
      <h2>通话记录</h2>
      <div class="search">
        <input id="fCaller" placeholder="搜电话号码">
        <div class="seg" id="kindSeg">
          <button type="button" data-kind="" class="on">全部</button>
          <button type="button" data-kind="电话">电话</button>
          <button type="button" data-kind="未接">未接</button>
          <button type="button" data-kind="留言">留言</button>
        </div>
        <input type="hidden" id="fKind" value="">
        <input id="fDate" type="date">
        <button class="btn btn-sm" onclick="searchList()">搜索</button>
        <input id="fDays" type="number" value="30" min="1" max="365" title="导出天数">
        <button class="btn btn-sm btn-ghost" onclick="exportXlsx()">导出 Excel</button>
      </div>
      <div id="list"><div class="empty">加载中…</div></div>
      <div class="pager">
        <button id="prevBtn" class="btn btn-sm btn-ghost" onclick="changePage(-1)">上一页</button>
        <button id="nextBtn" class="btn btn-sm btn-ghost" onclick="changePage(1)">下一页</button>
      </div>
    </div>
    <div>
      <div class="card detail-sticky" id="detail">
        <h2>记录详情</h2>
        <div class="empty">在左侧点一条记录查看详情、听录音、补填内容。<br>来电时新记录会自动弹到这里。</div>
      </div>
      <div class="card" style="margin-top:20px">
        <h2>我的统计与历史</h2>
        <div class="stats">
          <div class="stat"><b id="statToday">–</b><span>今日接听</span></div>
          <div class="stat"><b id="statTotal">–</b><span>累计接听</span></div>
          <div class="stat"><b id="statVm">–</b><span>分派留言</span></div>
        </div>
        <div id="history"><div class="empty">加载中…</div></div>
      </div>
    </div>
  </div>
</div>
<script>
var baseline = 0, currentId = null, offset = 0;
var SERVICE_TYPES = ["还车异常", "借车异常", "锁桩故障", "车辆损坏", "掉链", "乱停放", "扣费退款", "其他"];
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function beep() {
  try {
    var ctx = new (window.AudioContext || window.webkitAudioContext)();
    var o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = 880; g.gain.value = 0.15;
    o.start(); setTimeout(function() { o.stop(); ctx.close(); }, 600);
  } catch (e) {}
}
async function api(url, opts) {
  var r = await fetch(url, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("unauth"); }
  return r;
}
function kindPill(k) {
  var cls = k === "未接" ? "pill-red" : (k === "留言" ? "pill-amber" : "pill-green");
  return '<span class="pill ' + cls + '">' + esc(k) + '</span>';
}
function statusLabel(s) {
  if (s === "Available") return ["dot-green", "接单中"];
  if (s === "On Break") return ["dot-amber", "置忙"];
  if (s === "Logged Out") return ["dot-gray", "未登录"];
  return ["dot-gray", "未知"];
}
async function refreshMe() {
  var r = await api("/desk/me");
  var d = await r.json();
  $("seatLabel").textContent = d.seat;
  var st = statusLabel(d.status);
  var pill = $("statusPill");
  pill.className = "pill " + (st[0] === "dot-green" ? "pill-green" : (st[0] === "dot-amber" ? "pill-amber" : "pill-gray"));
  pill.textContent = st[1];
  if (d.status === "Available") $("busySwitch").checked = true;
  if (d.status === "On Break") $("busySwitch").checked = false;
  $("statToday").textContent = d.stats.calls_today;
  $("statTotal").textContent = d.stats.calls_total;
  $("statVm").textContent = d.stats.voicemails;
  $("history").innerHTML = d.history.length ? d.history.map(function(h) {
    return '<div class="hist">' + kindPill(h.kind) + " " + esc(h.caller) + " · " + esc(h.duration) +
      '<span class="t">　' + esc(h.time) + '</span></div>';
  }).join("") : '<div class="empty">暂无历史记录</div>';
}
async function refreshSeats() {
  var r = await api("/desk/statuses");
  var d = await r.json();
  $("seatStrip").innerHTML = Object.keys(d.agents).sort().map(function(s) {
    var st = statusLabel(d.agents[s]);
    return '<span class="seat-chip"><span class="dot ' + st[0] + '"></span>' + esc(s) + " " + st[1] + "</span>";
  }).join("");
}
async function refreshList() {
  var q = new URLSearchParams({
    caller: $("fCaller").value, kind: $("fKind").value,
    date: $("fDate").value, offset: String(offset), limit: "50"
  });
  var r = await api("/desk/records?" + q.toString());
  var d = await r.json();
  var rows = d.records || [];
  $("list").innerHTML = rows.length ? rows.map(function(rec) {
    var note = (rec.service_type ? esc(rec.service_type) : "") +
      (rec.question ? " · " + esc(rec.question) : "");
    return '<div class="row" onclick="openRecord(' + rec.record_id + ')">' +
      "<div>" + esc(rec.time) + "</div>" +
      "<div>" + kindPill(rec.kind) + "</div>" +
      '<div><span class="caller">' + esc(rec.caller) + "</span>" +
      '<div class="q">' + (note || "未填写") + "</div></div>" +
      "<div>" + esc(rec.agent || "") + "</div>" +
      "<div>" + esc(rec.duration) + "</div></div>";
  }).join("") : '<div class="empty">没有符合条件的记录</div>';
  $("prevBtn").disabled = offset <= 0;
  $("nextBtn").disabled = rows.length < 50;
}
function searchList() { offset = 0; refreshList(); }
function changePage(step) {
  offset = Math.max(0, offset + step * 50);
  refreshList();
}
function exportXlsx() {
  var days = parseInt($("fDays").value || "30", 10);
  location.href = "/desk/export.xlsx?days=" + days;
}
async function openRecord(id) {
  currentId = id;
  var r = await api("/desk/records/" + id);
  if (!r.ok) { $("detail").innerHTML = "<h2>记录详情</h2><div class='empty'>记录不存在</div>"; return; }
  var d = await r.json();
  var opts = SERVICE_TYPES.map(function(t) {
    return '<option value="' + esc(t) + '"' + (t === d.service_type ? " selected" : "") + ">" + esc(t) + "</option>";
  }).join("");
  var audio = d.has_recording
    ? '<audio controls src="/desk/records/' + d.record_id + '/recording"></audio>'
    : '<div style="color:var(--muted)">无录音</div>';
  var hint = d.history_hint
    ? '<div class="msg msg-info">' + esc(d.history_hint) + "</div>" : "";
  $("detail").innerHTML = "<h2>记录详情</h2>" + hint +
    "<dl>" +
    "<dt>登记时间</dt><dd>" + esc(d.time) + "</dd>" +
    "<dt>联系电话</dt><dd>" + esc(d.caller) + "</dd>" +
    "<dt>类型</dt><dd>" + kindPill(d.kind) + "</dd>" +
    "<dt>客服人员</dt><dd>" + esc(d.agent || d.owner_seat || "") + "</dd>" +
    "<dt>通话时长</dt><dd>" + esc(d.duration) + "</dd>" +
    "<dt>通话ID</dt><dd>" + esc(d.call_id) + "</dd>" +
    "<dt>录音</dt><dd>" + audio + "</dd>" +
    "</dl>" +
    "<label>服务类型</label><select id='fService'><option value=''></option>" + opts + "</select>" +
    "<label>通话内容（客户问的什么问题）</label>" +
    "<textarea id='fQuestion' rows='4'>" + esc(d.question) + "</textarea>" +
    "<button class='btn' style='margin-top:14px' onclick='saveNotes()'>保存</button> " +
    "<span id='saveMsg' style='color:var(--muted);font-size:14px'></span>";
  try { history.replaceState(null, "", "/desk?record=" + id); } catch (e) {}
}
async function saveNotes() {
  if (!currentId) return;
  var r = await api("/desk/records/" + currentId + "/notes", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ service_type: $("fService").value, question: $("fQuestion").value })
  });
  $("saveMsg").textContent = r.ok ? "已保存" : "保存失败";
  if (r.ok) refreshList();
}
$("busySwitch").onchange = async function() {
  var busy = !$("busySwitch").checked;
  var r = await api("/desk/busy", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ busy: busy })
  });
  if (!r.ok) { alert("设置失败：话务层可能没有运行"); $("busySwitch").checked = !busy; return; }
  refreshMe(); refreshSeats();
};
$("kindSeg").onclick = function(e) {
  var btn = e.target.closest ? e.target.closest("button") : null;
  if (!btn) return;
  var buttons = $("kindSeg").getElementsByTagName("button");
  for (var i = 0; i < buttons.length; i++) buttons[i].classList.remove("on");
  btn.classList.add("on");
  $("fKind").value = btn.getAttribute("data-kind");
  searchList();
};
async function pollFeed() {
  var r = await api("/desk/feed");
  var d = await r.json();
  var rows = d.records || [];
  if (!rows.length) return;
  if (baseline && rows[0].rowid > baseline) {
    var rec = rows[0];
    $("alertText").innerHTML = "📞 新" + esc(rec.kind) + " <b>" + esc(rec.caller) + "</b>　正在打开记录…";
    $("alert").classList.add("show");
    beep();
    openRecord(rec.rowid);
    refreshList();
  }
  baseline = rows[0].rowid;
}
refreshMe(); refreshSeats(); refreshList(); pollFeed();
setInterval(pollFeed, 3000);
setInterval(function() { refreshMe(); refreshSeats(); }, 30000);
var params = new URLSearchParams(location.search);
if (params.get("record")) { openRecord(parseInt(params.get("record"), 10)); }
</script>
"""
    return _page("工作台 - 自行车客服系统", body.replace("__SEAT__", html.escape(seat)),
                 extra_css=DESK_CSS)


# ------------------------------------------------------------------ routes


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/desk" if get_seat(request) else "/login")


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if get_seat(request):
        return RedirectResponse("/desk")
    return login_page(_ROTATION_AGENTS, bridge.store.has_agents())


@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request):
    form = await request.form()
    seat = (form.get("seat") or "").strip()
    password = form.get("password") or ""
    if seat not in _ROTATION_AGENTS or not bridge.store.verify_agent(seat, password):
        return login_page(_ROTATION_AGENTS, bridge.store.has_agents(),
                          error="坐席或密码不对，请重试。")
    token = bridge.store.create_session(seat)
    # Login = Available by default: calls go to this seat unless the
    # agent later sets themselves On Break. ESL down must not block
    # login (queue status then shows 未知）.
    bridge.set_agent_status(seat, "Available")
    resp = RedirectResponse("/desk", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=12 * 3600, path="/")
    return resp


@app.post("/logout")
async def logout_post(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    seat = bridge.store.delete_session(token) if token else None
    if seat:
        bridge.set_agent_status(seat, "Logged Out")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/setup", response_class=HTMLResponse)
def setup_get():
    return setup_page(_ROTATION_AGENTS)


@app.post("/setup", response_class=HTMLResponse)
async def setup_post(request: Request):
    form = await request.form()
    secret = form.get("secret") or ""
    if not BRIDGE_SECRET or secret != BRIDGE_SECRET:
        return setup_page(_ROTATION_AGENTS,
                          error="桥接密钥不对（或服务器还没有设置 BRIDGE_SECRET）。")
    changed, errors = [], []
    for seat in _ROTATION_AGENTS:
        pw = form.get(f"pw_{seat}") or ""
        pw2 = form.get(f"pw2_{seat}") or ""
        if not pw:
            continue
        if pw != pw2:
            errors.append(seat + " 两次密码不一致")
            continue
        bridge.store.set_agent_password(seat, pw)
        changed.append(seat)
    if errors:
        return setup_page(_ROTATION_AGENTS, error="；".join(errors))
    if not changed:
        return setup_page(_ROTATION_AGENTS, error="没有填任何新密码。")
    return setup_page(_ROTATION_AGENTS,
                      ok="已保存：" + "、".join(changed) + "。坐席可以用新密码在 / 登录。")


@app.get("/desk", response_class=HTMLResponse)
def desk_get(request: Request):
    seat = get_seat(request)
    if not seat:
        return RedirectResponse("/login")
    try:
        bridge.ensure_online(seat)
    except Exception:
        pass
    return desk_page_html(seat)


@app.get("/desk/feed")
def desk_feed(request: Request):
    require_seat(request)
    return {"records": bridge.store.recent_records(20)}


@app.get("/desk/me")
def desk_me(request: Request):
    seat = require_seat(request)
    try:
        bridge.ensure_online(seat)
    except Exception:
        pass
    statuses = bridge.get_agent_statuses()
    data = bridge.get_agent_history(seat)
    data["status"] = statuses.get(seat, "unknown")
    return data


@app.get("/desk/statuses")
def desk_statuses(request: Request):
    require_seat(request)
    return {"agents": bridge.get_agent_statuses()}


@app.post("/desk/busy")
async def desk_busy(request: Request):
    seat = require_seat(request)
    body = await request.json()
    target = "On Break" if body.get("busy") else "Available"
    ok, status = bridge.set_agent_status(seat, target)
    if not ok:
        raise HTTPException(status_code=502, detail=status)
    return {"seat": seat, "status": status}


@app.get("/desk/records")
def desk_records(request: Request, caller: str = "", kind: str = "",
                 date: str = "", offset: int = 0, limit: int = 50):
    require_seat(request)
    return {"records": bridge.store.list_records(
        caller=caller, kind=kind, date_from=date, date_to=date,
        offset=offset, limit=limit)}


@app.get("/desk/records/{record_id}")
def desk_record(request: Request, record_id: int):
    require_seat(request)
    rec = bridge.store.get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="record not found")
    rec["history_hint"] = summarize_call_history(
        bridge.store.calls_for_caller(rec["caller"],
                                      exclude_call_id=rec["call_id"]))
    return rec


@app.post("/desk/records/{record_id}/notes")
async def desk_record_notes(request: Request, record_id: int):
    require_seat(request)
    body = await request.json()
    ok = bridge.store.set_notes(record_id, body.get("question") or "",
                                body.get("service_type") or "")
    if not ok:
        raise HTTPException(status_code=404, detail="record not found")
    return {"ok": True}


@app.get("/desk/records/{record_id}/recording")
def desk_record_recording(request: Request, record_id: int):
    require_seat(request)
    rec = bridge.store.get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="record not found")
    path = find_recording_file(rec.get("recording_file") or "")
    if not path:
        raise HTTPException(status_code=404, detail="no recording")
    return Response(open(path, "rb").read(), media_type="audio/wav")


@app.get("/desk/export.xlsx")
def desk_export(request: Request, days: int = 30):
    require_seat(request)
    days = max(1, min(days, 365))
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    rows = [call_row(r) for r in bridge.store.list_calls(start)]
    stamp = datetime.now().strftime("%Y%m%d")
    return _xlsx_response(build_xlsx([("通话记录", CALL_COLUMNS, rows)]),
                          f"records_{stamp}.xlsx")


# Old bookmarks keep working: everything now lives on the desk.
@app.get("/board", response_class=HTMLResponse)
def board_legacy():
    return RedirectResponse("/desk")


@app.get("/records", response_class=HTMLResponse)
def records_legacy():
    return RedirectResponse("/desk")


@app.get("/records/{record_id}", response_class=HTMLResponse)
def record_legacy(record_id: int):
    return RedirectResponse(f"/desk?record={record_id}")

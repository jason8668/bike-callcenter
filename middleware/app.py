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
radial-gradient(700px 460px at 10% -5%, rgba(0,122,255,.22), transparent 60%),
radial-gradient(560px 460px at 95% 0%, rgba(175,82,222,.20), transparent 60%),
radial-gradient(520px 420px at 88% 55%, rgba(255,45,85,.14), transparent 60%),
radial-gradient(640px 480px at 8% 60%, rgba(52,199,123,.16), transparent 60%),
radial-gradient(720px 520px at 50% 115%, rgba(255,149,0,.16), transparent 60%)}
.topbar,.wrap,.center{position:relative;z-index:1}
a{color:var(--blue)}
.topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:14px;
padding:12px 20px;flex-wrap:wrap;
background:rgba(255,255,255,.72);backdrop-filter:blur(20px) saturate(180%);
-webkit-backdrop-filter:blur(20px) saturate(180%);
border-bottom:1px solid rgba(0,0,0,.06)}
.topbar .brand{font-size:19px;font-weight:700;letter-spacing:-.3px;display:flex;align-items:center;gap:10px}
.logo{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;flex:none;
border-radius:10px;background:linear-gradient(135deg,#007aff,#5856d6,#ff2d55);font-size:19px;
box-shadow:0 2px 10px rgba(88,86,214,.35)}
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
.wrap{max-width:1400px;margin:0 auto;padding:20px}
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
.hero{width:100%;height:190px;object-fit:cover;border-radius:16px;margin-bottom:14px;display:block}
.hero-sm{width:100%;height:120px;object-fit:cover;border-radius:16px;margin-bottom:14px;display:block}
.msg{border-radius:14px;padding:11px 15px;font-size:14px;margin:12px 0;line-height:1.6}
.msg-err{background:var(--red-bg);color:#d70015}
.msg-ok{background:var(--green-bg);color:#248a3d}
.msg-info{background:rgba(0,122,255,.10);color:#0057b8}
.foot{font-size:13px;color:var(--muted);text-align:center;margin-top:16px}
"""

DESK_CSS = """
.grid{grid-template-columns:1.05fr 1.3fr 0.85fr}
.alert{display:none;border-radius:24px;padding:16px 22px;margin-bottom:20px;
background:rgba(255,59,48,.88);color:#fff;backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
box-shadow:0 8px 28px rgba(255,59,48,.35)}
.alert.show{display:block;animation:flash 1.2s infinite}
@keyframes flash{0%,100%{box-shadow:0 8px 28px rgba(255,59,48,.35)}50%{box-shadow:0 8px 36px rgba(255,59,48,.55)}}
.alert b{font-size:22px}
.alert small{font-size:14px;opacity:.9}
.queue-warn{display:none;border-radius:16px;padding:12px 18px;margin-bottom:20px;
background:rgba(255,149,0,.14);color:#8a4b00;border:1px solid rgba(255,149,0,.25);
font-size:14px;line-height:1.6}
.queue-warn.show{display:block}
.search{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.search input,.search select{width:auto;flex:1;min-width:110px}
.seg{display:flex;background:var(--gray-bg);border-radius:10px;padding:2px;gap:2px}
.seg button{border:0;background:transparent;padding:7px 14px;border-radius:8px;font-size:14px;
cursor:pointer;color:var(--text);box-shadow:none}
.seg button.on{background:#007aff;color:#fff;box-shadow:0 1px 6px rgba(0,122,255,.4);font-weight:600}
.switch-wrap{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:500}
.switch{position:relative;display:inline-block;width:51px;height:31px;flex:none}
.switch input{display:none}
.switch .slider{position:absolute;inset:0;background:rgba(120,120,128,.32);border-radius:999px;
transition:.2s;cursor:pointer}
.switch .slider:before{content:"";position:absolute;width:27px;height:27px;left:2px;top:2px;
background:#fff;border-radius:50%;box-shadow:0 2px 6px rgba(0,0,0,.25);transition:.2s}
.switch input:checked + .slider{background:#34c759}
.switch input:checked + .slider:before{transform:translateX(20px)}
.row{display:block;padding:11px 10px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:rgba(0,0,0,.02)}
.row:active{background:rgba(0,0,0,.04)}
.row-top{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--muted)}
.row-top .dur{margin-left:auto}
.row .caller{font-weight:600;font-size:15px;margin-top:3px}
.row .q{color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.seats{display:flex;gap:10px;flex-wrap:wrap}
.seat-chip{border:1px solid rgba(255,255,255,.8);background:rgba(255,255,255,.55);
border-radius:999px;padding:6px 14px;font-size:14px}
.seat-note{color:var(--muted);font-size:13px;margin-top:10px;line-height:1.6}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.dot-green{background:#34c759}.dot-amber{background:#ff9500}.dot-gray{background:#8a8a8e}
.stats{display:flex;gap:28px;margin-bottom:8px}
.stat b{font-size:28px;display:block;letter-spacing:-.5px}
.stat-blue b{color:#007aff}
.stat-green b{color:#248a3d}
.stat-purple b{color:#5856d6}
.stat span{color:var(--muted);font-size:13px}
.hist{font-size:14px;padding:8px 0;border-bottom:1px solid var(--line)}
.hist .t{color:var(--muted);font-size:13px}
dl{margin:0}
dt{color:var(--muted);font-size:13px;margin-top:10px}
dd{margin:2px 0 0;font-size:15px}
audio{width:100%;margin-top:8px}
.pager{display:flex;gap:8px;justify-content:space-between;margin-top:14px}
.empty{color:var(--muted);text-align:center;padding:30px 0}
.hero-empty{width:230px;max-width:80%;border-radius:16px;display:block;margin:0 auto 14px}
.detail-sticky{position:sticky;top:68px}
.right-col{display:flex;flex-direction:column;gap:20px}
@media(max-width:1100px){.grid{grid-template-columns:1fr}.detail-col{order:-1}.detail-sticky{position:static}}
"""


HERO_IMG = "data:image/jpeg;base64,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"


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
            "<img class=\"hero\" src=\"" + HERO_IMG + "\">"
            "<h1>自行车客服工作台</h1>"
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
            "<img class=\"hero-sm\" src=\"" + HERO_IMG + "\">"
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
  <div class="brand"><span class="logo">🚲</span> 自行车客服工作台</div>
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
  <div id="queueWarn" class="queue-warn">⚠️ 话务层未连接（FreeSWITCH 还没装好）：接单开关和坐席状态暂时不可用，登录也不会显示接单中；记录、补填、导出都正常。等话务层装好后这条提示自动消失。</div>
  <div class="grid">
    <div class="card list-col">
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
    <div class="card detail-col detail-sticky" id="detail">
      <h2>记录详情</h2>
      <div class="empty"><img class="hero-empty" src="__HERO__">在左侧点一条记录查看详情、听录音、补填内容。<br>来电时新记录会自动弹到这里。</div>
    </div>
    <div class="right-col">
      <div class="card">
        <h2>坐席状态</h2>
        <div id="seatStrip" class="seats"><span style="color:var(--muted)">加载中…</span></div>
        <div class="seat-note">状态由登录自动决定，不用选也不能改别人的：登录即接单中，自己置忙变置忙，退出变未登录。</div>
      </div>
      <div class="card">
        <h2>我的统计与历史</h2>
        <div class="stats">
          <div class="stat stat-blue"><b id="statToday">–</b><span>今日接听</span></div>
          <div class="stat stat-green"><b id="statTotal">–</b><span>累计接听</span></div>
          <div class="stat stat-purple"><b id="statVm">–</b><span>分派留言</span></div>
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
  var keys = Object.keys(d.agents);
  var allUnknown = keys.length > 0 && keys.every(function(s) { return d.agents[s] === "unknown"; });
  $("queueWarn").classList.toggle("show", allUnknown);
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
      '<div class="row-top"><span>' + esc(rec.time) + "</span>" + kindPill(rec.kind) +
      "<span>" + esc(rec.agent || "") + '</span><span class="dur">' + esc(rec.duration) + "</span></div>" +
      '<div class="caller">' + esc(rec.caller) + "</div>" +
      '<div class="q">' + (note || "未填写") + "</div></div>";
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
  if (!r.ok) { alert("置忙失败：话务层还没运行（FreeSWITCH 未安装），等话务层装好后开关才能用。记录、补填、导出不受影响。"); $("busySwitch").checked = !busy; return; }
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
    return _page("工作台 - 自行车客服系统",
                 body.replace("__SEAT__", html.escape(seat)).replace("__HERO__", HERO_IMG),
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

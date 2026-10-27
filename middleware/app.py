"""Record service entrypoint (FastAPI).

Run: uvicorn app:app --host 0.0.0.0 --port 8090
Env: see .env.example. All secrets live in .env only, never in Git.

This is a RECORD system, not a ticket system: no Zammad, no workflow.
Pages: /records (record list), /records/<id> (detail + notes + audio),
/board (agent pop-up board), /test (simulated calls without a line).
"""
import html
import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

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
弹出来电提示，接通时记录就自动弹出来（带电话号码），坐席可以在通话过程中
记录问题；通话结束后录音自动绑到这条记录上。来电不带任何预分类：记录先
进入记录列表，由坐席在记录里填写服务类型等信息。</p>

<label>客户手机号码</label>
<input id="caller" value="13800000000">

<label>访问密钥（服务器 middleware/.env 里的 BRIDGE_SECRET）</label>
<input id="secret" type="password" placeholder="第一次使用时粘贴一次即可">

<button id="go">发起模拟来电</button>
<div id="result"></div>

<p class="hint">操作顺序：先打开坐席来电看板（/board）并点“开始值班”，
再回到本页点按钮，然后切回看板页面看弹屏和新记录。</p>

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
        const link = "/records/" + data.record_id;
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
        const link = "/records/" + data.record_id;
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


# --- agent board: a record pops open by itself, not a list badge --------
# Why this page exists: a record list never opens itself - a new record
# only shows up as a new row, and the agent has to notice it and click
# through. During a live call that is too slow. So agents keep this board
# open: it polls the bridge, and the moment a new record is created it
# navigates THIS tab straight to the record detail (self-navigation is
# not blocked by popup blockers). After finishing the record, the agent
# returns to the board bookmark; the next call then pops open the same way.
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
<p class="hint">上班时打开这个页面并点“开始值班”，来电时页面会自动跳到新记录，不用盯着列表等。
记录打开后这个标签页就变成记录页了，处理完请点收藏夹回到看板，下一通电话继续自动弹。</p>

<label>访问密钥（服务器 middleware/.env 里的 BRIDGE_SECRET）</label>
<input id="secret" type="password" placeholder="第一次使用时粘贴一次即可">

<button id="go">开始值班</button>
<div id="status">还没有开始值班。</div>
<div id="alert"></div>
<div id="recent"></div>

<h2 style="font-size:16px;margin-top:28px;">我的坐席状态</h2>
<p class="hint">有事离开时把自己置忙，队列和留言就不再分给你，只分给其他坐席；回来点“置闲”恢复。
也可以直接在话机上拨 *51 置忙、*52 置闲。</p>
<label>我是几号坐席</label>
<select id="seat" style="width:100%;padding:9px;font-size:15px;">
  <option value="8001">8001</option>
  <option value="8002">8002</option>
  <option value="8003">8003</option>
</select>
<button id="busyBtn" style="background:#666;">置忙</button>
<div id="seatStatus" style="margin-top:12px;font-size:14px;line-height:1.9;"></div>

<h2 style="font-size:16px;margin-top:28px;">我的接单统计与历史</h2>
<p class="hint">这里是你自己接过的电话和分给你的留言：今日接听、累计接听、分派留言数，
下面是最近 50 条记录（点记录号可打开原记录）。</p>
<div id="myStats" style="margin-top:12px;font-size:14px;line-height:1.9;"></div>
<div id="myHistory" style="margin-top:12px;font-size:14px;line-height:1.9;"></div>

<script>
let baseline = 0, watching = false, timer = null, audioCtx = null;
const params = new URLSearchParams(location.search);
const saved = params.get("secret") || localStorage.getItem("bridge_secret") || "";
if (saved) document.getElementById("secret").value = saved;

function recordUrl(id) {
  return "/records/" + id;
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
  const rows = data.records || [];
  document.getElementById("recent").innerHTML = rows.map(r =>
    "<div>" + r.created_at + " " + (r.kind || "电话") + " " + r.caller +
    " <a target='_blank' href='" + recordUrl(r.rowid) + "'>记录 #" + r.rowid + "</a></div>"
  ).join("") || "暂无记录。";
  if (!rows.length) return;
  const newest = rows[0];
  if (newest.rowid > baseline) {
    baseline = newest.rowid;
    const box = document.getElementById("alert");
    box.style.display = "block";
    box.textContent = "新来电 " + newest.caller + "，正在打开记录 #" + newest.rowid + "…";
    document.getElementById("status").textContent = "新来电，正在打开记录…";
    beep();
    setTimeout(() => { location.href = recordUrl(newest.rowid); }, 2000);
  }
}
const seatSel = document.getElementById("seat");
seatSel.value = localStorage.getItem("board_seat") || "8001";
seatSel.onchange = () => { localStorage.setItem("board_seat", seatSel.value); refreshSeat(); refreshHistory(); };
async function refreshSeat() {
  const secret = document.getElementById("secret").value.trim();
  if (!secret) return;
  let resp;
  try { resp = await fetch("/agents/status", { headers: { "X-Bridge-Secret": secret } }); }
  catch (e) { return; }
  if (!resp.ok) return;
  const data = await resp.json();
  const agents = data.agents || {};
  const mine = agents[seatSel.value];
  const btn = document.getElementById("busyBtn");
  btn.textContent = mine === "On Break" ? "置闲（恢复接单）" : "置忙";
  btn.style.background = mine === "On Break" ? "#0e7a5f" : "#666";
  const label = s => s === "Available" ? "接单中" : s === "On Break" ? "置忙" :
    s === "Logged Out" ? "未登录" : "未知";
  document.getElementById("seatStatus").innerHTML = Object.keys(agents).map(a =>
    "<div>" + a + "：" + label(agents[a]) + (a === seatSel.value ? "（我）" : "") + "</div>"
  ).join("") || "状态暂时查不到（话务层没接通时不影响看板弹单）。";
}
async function refreshHistory() {
  const secret = document.getElementById("secret").value.trim();
  if (!secret) return;
  let resp;
  try { resp = await fetch("/agents/me?seat=" + encodeURIComponent(seatSel.value),
    { headers: { "X-Bridge-Secret": secret } }); }
  catch (e) { return; }
  if (!resp.ok) return;
  const data = await resp.json();
  const s = data.stats || {};
  document.getElementById("myStats").innerHTML =
    "今日接听 " + (s.calls_today || 0) + " 通 · 累计接听 " + (s.calls_total || 0) +
    " 通 · 分派留言 " + (s.voicemails || 0) + " 条";
  const rows = data.history || [];
  document.getElementById("myHistory").innerHTML = rows.map(r =>
    "<div>" + (r.time || "") + " " + r.kind + " " + r.caller + " " + (r.duration || "") +
    (r.record_id ? " <a target='_blank' href='" + recordUrl(r.record_id) +
      "'>记录 #" + r.record_id + "</a>" : "") + "</div>"
  ).join("") || "暂无记录。";
}
document.getElementById("busyBtn").onclick = async () => {
  const secret = document.getElementById("secret").value.trim();
  if (!secret) { alert("请先在上面填访问密钥。"); return; }
  const busy = document.getElementById("busyBtn").textContent === "置忙";
  const resp = await fetch("/agents/busy", { method: "POST",
    headers: { "X-Bridge-Secret": secret, "Content-Type": "application/json" },
    body: JSON.stringify({ agent: seatSel.value, busy }) });
  if (!resp.ok) { alert("设置失败，请看话务层是否在运行。"); return; }
  localStorage.setItem("board_seat", seatSel.value);
  refreshSeat();
};
document.getElementById("go").onclick = async () => {
  const secret = document.getElementById("secret").value.trim();
  localStorage.setItem("bridge_secret", secret);
  localStorage.setItem("board_seat", seatSel.value);
  try { if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {}
  document.getElementById("status").textContent = "值班中，正在等来电…";
  document.getElementById("go").disabled = true;
  const resp = await fetch("/board/feed", { headers: { "X-Bridge-Secret": secret } });
  if (resp.ok) {
    const data = await resp.json();
    const rows = data.records || [];
    baseline = rows.length ? rows[0].rowid : 0;
  }
  watching = true;
  timer = setInterval(poll, 2000);
  poll();
  refreshSeat();
  refreshHistory();
  setInterval(refreshSeat, 10000);
  setInterval(refreshHistory, 10000);
};
</script>
</body>
</html>
"""


@app.get("/board", response_class=HTMLResponse)
def board_page():
    """Agent board: keep open on shift; new records open themselves."""
    return BOARD_PAGE_HTML


@app.get("/board/feed")
def board_feed(x_bridge_secret: str | None = Header(default=None)):
    """Newest customer records, newest first (secret-protected)."""
    check_secret(x_bridge_secret)
    return {"records": bridge.store.recent_records(20)}


@app.get("/agents/status")
def agents_status(x_bridge_secret: str | None = Header(default=None)):
    """Queue status per roster seat (secret-protected)."""
    check_secret(x_bridge_secret)
    return {"agents": bridge.get_agent_statuses()}


@app.get("/agents/me")
def agents_me(seat: str, x_bridge_secret: str | None = Header(default=None)):
    """One seat's homepage data: calls answered (today / total),
    voicemails dispatched, and handled-contact history (secret-
    protected). Seat must be on the roster."""
    check_secret(x_bridge_secret)
    if seat not in bridge.rotation_agents:
        raise HTTPException(status_code=400, detail="unknown seat")
    return bridge.get_agent_history(seat)


@app.post("/agents/busy")
async def agents_busy(request: Request, x_bridge_secret: str | None = Header(default=None)):
    """Set one seat On Break (busy) or Available (secret-protected).
    Body: {"agent": "8001", "busy": true}. Busy seats are skipped by
    the queue and by voicemail dispatch until set back."""
    check_secret(x_bridge_secret)
    try:
        body = await request.json()
    except Exception:
        body = {}
    agent = str((body or {}).get("agent") or "")
    busy = bool((body or {}).get("busy"))
    if agent not in bridge.rotation_agents:
        raise HTTPException(status_code=400, detail="unknown seat")
    ok, status = bridge.set_agent_busy(agent, busy)
    if not ok:
        raise HTTPException(status_code=502, detail=status)
    return {"ok": True, "agent": agent, "status": status}


# ------------------------------------------------------------------ records
# The record system itself: list, detail, notes, audio. No workflow,
# no states, no dispatch queue - a record is one row per contact.

RECORDS_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>通话记录 - 自行车客服系统</title>
<style>
  body { font-family: sans-serif; margin: 24px auto; max-width: 760px; padding: 0 16px; color: #222; }
  h1 { font-size: 20px; }
  label { display: block; margin: 10px 0 4px; font-size: 14px; }
  input, select { width: 100%; padding: 9px; font-size: 15px; box-sizing: border-box; }
  button { margin-top: 16px; padding: 11px 18px; font-size: 15px;
           background: #0e7a5f; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }
  button:disabled { background: #999; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
  .hint { color: #666; font-size: 13px; line-height: 1.6; }
  #list { margin-top: 20px; font-size: 14px; line-height: 1.9; }
  #list a { color: #0e7a5f; }
  #pager { margin-top: 16px; }
  #pager button { background: #1a5fb4; margin-right: 10px; }
  #msg { margin-top: 14px; font-size: 14px; color: #666; }
</style>
</head>
<body>
<h1>通话记录</h1>
<p class="hint">这是全部通话记录：电话、未接、留言都在这里。点一条打开详情，
可以听录音、补填客户问的什么问题。坐席值班弹屏请用 <a href="/board">来电看板</a>。</p>

<label>访问密钥（服务器 middleware/.env 里的 BRIDGE_SECRET）</label>
<input id="secret" type="password" placeholder="第一次使用时粘贴一次即可">

<div class="row">
<div><label>客户电话（模糊搜）</label><input id="caller" placeholder="比如 138"></div>
<div><label>坐席</label><select id="agent">
  <option value="">全部坐席</option>
  <option value="8001">8001</option>
  <option value="8002">8002</option>
  <option value="8003">8003</option>
</select></div>
<div><label>类型</label><select id="kind">
  <option value="">全部类型</option>
  <option value="电话">电话</option>
  <option value="未接">未接</option>
  <option value="留言">留言</option>
</select></div>
<div><label>导出范围（最近多少天）</label><input id="exp_days" value="30"></div>
<div><label>开始日期</label><input id="date_from" type="date"></div>
<div><label>结束日期</label><input id="date_to" type="date"></div>
</div>

<button id="search">搜索</button>
<button id="export" style="background:#1a5fb4">导出 Excel</button>
<div id="msg"></div>
<div id="list"></div>
<div id="pager">
  <button id="prev">上一页</button>
  <button id="next">下一页</button>
  <span id="pageInfo" style="font-size:14px;"></span>
</div>

<script>
let offset = 0;
const LIMIT = 50;
const params = new URLSearchParams(location.search);
const saved = params.get("secret") || localStorage.getItem("bridge_secret") || "";
if (saved) document.getElementById("secret").value = saved;

function query() {
  const q = new URLSearchParams();
  const v = id => document.getElementById(id).value.trim();
  if (v("caller")) q.set("caller", v("caller"));
  if (v("agent")) q.set("agent", v("agent"));
  if (v("kind")) q.set("kind", v("kind"));
  if (v("date_from")) q.set("date_from", v("date_from"));
  if (v("date_to")) q.set("date_to", v("date_to"));
  q.set("limit", LIMIT); q.set("offset", offset);
  return q.toString();
}
async function load() {
  const secret = document.getElementById("secret").value.trim();
  localStorage.setItem("bridge_secret", secret);
  const msg = document.getElementById("msg");
  const list = document.getElementById("list");
  msg.textContent = "正在查...";
  let resp;
  try {
    resp = await fetch("/records/feed?" + query(), { headers: { "X-Bridge-Secret": secret } });
  } catch (e) { msg.textContent = "请求失败：" + e; return; }
  if (resp.status === 401) { msg.textContent = "密钥不对，请重新粘贴访问密钥。"; list.innerHTML = ""; return; }
  if (!resp.ok) { msg.textContent = "查询失败：HTTP " + resp.status; return; }
  const data = await resp.json();
  const rows = data.records || [];
  msg.textContent = rows.length ? "共 " + rows.length + " 条（本页）" : "没有符合条件的记录。";
  document.getElementById("pageInfo").textContent = "第 " + (offset / LIMIT + 1) + " 页";
  document.getElementById("prev").disabled = offset === 0;
  document.getElementById("next").disabled = rows.length < LIMIT;
  list.innerHTML = rows.map(r =>
    "<div>" + (r.time || "") + " " + r.kind + " " + r.caller +
    "（" + (r.agent || "未分派") + "，" + (r.duration || "") + "）" +
    (r.service_type ? " " + r.service_type : "") +
    (r.question ? " " + r.question.slice(0, 30) : "") +
    " <a target='_blank' href='/records/" + r.record_id + "'>记录 #" + r.record_id + "</a></div>"
  ).join("");
}
document.getElementById("search").onclick = () => { offset = 0; load(); };
document.getElementById("prev").onclick = () => { offset = Math.max(0, offset - LIMIT); load(); };
document.getElementById("next").onclick = () => { offset += LIMIT; load(); };
document.getElementById("export").onclick = async () => {
  const secret = document.getElementById("secret").value.trim();
  localStorage.setItem("bridge_secret", secret);
  const days = document.getElementById("exp_days").value.trim() || "30";
  const msg = document.getElementById("msg");
  try {
    const resp = await fetch("/export/calls.xlsx?days=" + encodeURIComponent(days), {
      headers: { "X-Bridge-Secret": secret }
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "records.xlsx";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    msg.textContent = "导出成功，文件已开始下载。";
  } catch (e) {
    msg.textContent = "导出失败：" + e + "（常见原因：密钥不对）";
  }
};
load();
</script>
</body>
</html>
"""

SERVICE_TYPE_OPTIONS = ["", "还车异常", "借车异常", "锁桩故障", "车辆损坏",
                        "掉链", "乱停放", "扣费退款", "其他"]


def record_detail_page(rec: dict, history_hint: str) -> str:
    """Server-rendered record detail: fields, audio player, notes form."""
    esc = html.escape
    channel = {"电话": "热线电话", "留言": "语音留言",
               "未接": "热线电话（未接）"}.get(rec["kind"], "热线电话")
    if rec["has_recording"]:
        audio = (f'<audio controls src="/records/{rec["record_id"]}/recording" '
                 'style="width:100%;"></audio>')
    else:
        audio = "无录音"
    options = ""
    for opt in SERVICE_TYPE_OPTIONS:
        sel = " selected" if opt == rec["service_type"] else ""
        options += f'<option value="{esc(opt)}"{sel}>{esc(opt) or "（未填）"}</option>'
    hint = (f'<p class="hint" style="color:#555;">历史提示：{esc(history_hint)}</p>'
            if history_hint else "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>记录 #{rec["record_id"]} - 自行车客服系统</title>
<style>
  body {{ font-family: sans-serif; margin: 24px auto; max-width: 560px; padding: 0 16px; color: #222; }}
  h1 {{ font-size: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 15px; }}
  td {{ padding: 8px 6px; border-bottom: 1px solid #eee; vertical-align: top; }}
  td:first-child {{ color: #666; width: 96px; }}
  label {{ display: block; margin: 14px 0 6px; font-size: 14px; }}
  input, select, textarea {{ width: 100%; padding: 9px; font-size: 15px; box-sizing: border-box; }}
  button {{ margin-top: 20px; width: 100%; padding: 12px; font-size: 16px;
           background: #0e7a5f; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }}
  button:disabled {{ background: #999; }}
  .hint {{ color: #666; font-size: 13px; line-height: 1.6; }}
  #result {{ display: none; margin-top: 16px; padding: 12px; border-radius: 6px; font-size: 14px; }}
  #result.ok {{ display: block; background: #e6f4ea; }}
  #result.err {{ display: block; background: #fdecea; }}
</style>
</head>
<body>
<h1>通话记录 #{rec["record_id"]}</h1>
<p><a href="/records">← 返回记录列表</a></p>
<table>
<tr><td>登记时间</td><td>{esc(rec["time"] or rec["created_at"])}</td></tr>
<tr><td>联系电话</td><td>{esc(rec["caller"])}</td></tr>
<tr><td>客服人员</td><td>{esc(rec["agent"] or rec["owner_seat"])}</td></tr>
<tr><td>通话时长</td><td>{esc(rec["duration"])}</td></tr>
<tr><td>服务途径</td><td>{esc(channel)}</td></tr>
<tr><td>服务类型</td><td>{esc(rec["service_type"]) or "（未填）"}</td></tr>
<tr><td>通话内容</td><td>{esc(rec["question"]) or "（未填）"}</td></tr>
<tr><td>录音文件</td><td>{audio}</td></tr>
<tr><td>通话ID</td><td>{esc(rec["call_id"])}</td></tr>
</table>
{hint}
<h2 style="font-size:16px;margin-top:28px;">补填：客户问的什么问题</h2>
<p class="hint">通话中或通话后在这里补填，保存后这条记录就完整了。</p>
<label>服务类型</label>
<select id="stype">{options}</select>
<label>通话内容（客户问的什么问题）</label>
<textarea id="question" rows="4">{esc(rec["question"])}</textarea>
<label>访问密钥（服务器 middleware/.env 里的 BRIDGE_SECRET）</label>
<input id="secret" type="password" placeholder="第一次使用时粘贴一次即可">
<button id="save">保存</button>
<div id="result"></div>
<script>
  const saved = localStorage.getItem("bridge_secret") || "";
  if (saved) document.getElementById("secret").value = saved;
  document.getElementById("save").addEventListener("click", async () => {{
    const btn = document.getElementById("save");
    const box = document.getElementById("result");
    const secret = document.getElementById("secret").value.trim();
    localStorage.setItem("bridge_secret", secret);
    btn.disabled = true; btn.textContent = "正在保存...";
    box.style.display = "none";
    try {{
      const resp = await fetch("/records/{rec["record_id"]}/notes", {{
        method: "POST",
        headers: {{"Content-Type": "application/json", "X-Bridge-Secret": secret}},
        body: JSON.stringify({{
          question: document.getElementById("question").value,
          service_type: document.getElementById("stype").value
        }})
      }});
      const data = await resp.json();
      box.style.display = "block";
      if (resp.ok && data.ok) {{
        box.className = "ok";
        box.textContent = "已保存。";
        setTimeout(() => location.reload(), 600);
      }} else {{
        box.className = "err";
        box.textContent = "保存失败：" + JSON.stringify(data);
      }}
    }} catch (e) {{
      box.style.display = "block"; box.className = "err";
      box.textContent = "请求失败：" + e;
    }}
    btn.disabled = false; btn.textContent = "保存";
  }});
</script>
</body>
</html>"""


@app.get("/records", response_class=HTMLResponse)
def records_page():
    """Record list page: search + browse all call records."""
    return RECORDS_PAGE_HTML


@app.get("/records/feed")
def records_feed(caller: str = "", agent: str = "", kind: str = "",
                 date_from: str = "", date_to: str = "",
                 limit: int = 50, offset: int = 0,
                 x_bridge_secret: str | None = Header(default=None)):
    """Record rows as JSON, newest first (secret-protected).
    Filters: caller substring, agent seat, kind 电话/未接/留言,
    date_from/date_to as YYYY-MM-DD (Asia/Shanghai)."""
    check_secret(x_bridge_secret)
    return {"records": bridge.store.list_records(
        caller, agent, kind, date_from, date_to, limit, offset)}


@app.get("/records/{record_id}", response_class=HTMLResponse)
def record_page(record_id: int):
    """One record's detail: fields, recording player, notes form."""
    rec = bridge.store.get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="record not found")
    try:
        hint = summarize_call_history(
            bridge.store.calls_for_caller(rec["caller"], rec["call_id"]))
    except Exception:
        hint = ""
    return record_detail_page(rec, hint)


@app.get("/records/{record_id}/recording")
def record_recording(record_id: int):
    """The record's recording audio (from the shared recordings volume)."""
    rec = bridge.store.get_record(record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="record not found")
    path = find_recording_file(rec["recording_file"])
    if not path:
        raise HTTPException(status_code=404, detail="no recording file")
    with open(path, "rb") as f:
        data = f.read()
    return Response(content=data, media_type="audio/wav",
                    headers={"Content-Disposition": f"inline; filename={record_id}.wav"})


@app.post("/records/{record_id}/notes")
async def record_notes(record_id: int, request: Request,
                       x_bridge_secret: str | None = Header(default=None)):
    """Agent fills in the question + service type (secret-protected)."""
    check_secret(x_bridge_secret)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ok = bridge.store.set_notes(
        record_id,
        str((body or {}).get("question") or ""),
        str((body or {}).get("service_type") or ""))
    if not ok:
        raise HTTPException(status_code=404, detail="record not found")
    return {"ok": True, "record_id": record_id}

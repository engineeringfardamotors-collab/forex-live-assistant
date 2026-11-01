"""داشبورد محلی کنترل دستیار با کتابخانه‌های استاندارد پایتون."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


HTML = """<!doctype html><html lang='fa' dir='rtl'><meta charset='utf-8'>
<title>Forex Live Assistant</title><style>
body{font-family:Segoe UI,Tahoma,sans-serif;background:#071126;color:#eaf2ff;max-width:900px;margin:40px auto;padding:0 20px}
.card{background:#0d1d3a;border:1px solid #24446f;border-radius:14px;padding:22px;margin:14px 0}
button{background:#2388ff;color:white;border:0;border-radius:8px;padding:11px 18px;margin:5px;cursor:pointer}button.danger{background:#c83d58}
input{background:#09152b;border:1px solid #36547d;color:white;padding:11px;border-radius:8px;width:65%}
pre{white-space:pre-wrap;color:#a9d7ff}.ok{color:#65e6a2}.warn{color:#ffd166}</style>
<h1>دستیار جلسات لایو فارکس <span id='mode' class='warn'></span></h1><div class='card'><h2>وضعیت</h2><pre id='status'>در حال دریافت...</pre>
<button onclick='refresh()'>به‌روزرسانی</button><button class='danger' onclick='endMeeting()'>پایان جلسه</button></div>
<div class='card'><h2>هشدارهای سلامت ضبط</h2><pre id='health' class='ok'>—</pre></div>
<div class='card'><h2>تأخیر مراحل سیگنال (میانگین / بیشینه)</h2><pre id='latency'>—</pre></div>
<div class='card'><h2>دستگاه ضبط صدا</h2><select id='adev' style='background:#09152b;color:white;border:1px solid #36547d;padding:8px;border-radius:8px;width:65%'></select><button onclick='setDevice()'>اعمال</button> <span id='adevmsg'></span></div>
<div class='card'><h2>شروع دستی جلسه</h2><input id='url' placeholder='https://...'><button onclick='startMeeting()'>بازکردن و شروع ضبط</button></div>
<div class='card'><h2>تست آفلاین</h2><p>بدون API، تلگرام یا میکروفون؛ فقط جریان تولید پیام را بررسی می‌کند.</p><button onclick='simulate()'>اجرای تست</button><pre id='result'></pre></div>
<script>
async function api(path, opts={}){let r=await fetch(path,opts);return await r.json()}
async function refresh(){let x=await api('/api/status');document.querySelector('#status').textContent=JSON.stringify(x,null,2);
let w=x.health_warnings||[];let h=document.querySelector('#health');
h.textContent=w.length?w.map(v=>'⚠️ '+v.message+' — '+v.for_sec+'s').join('\n'):'✔ مشکلی نیست';
h.className=w.length?'warn':'ok';
let st=(x.live&&x.live.latency_stages)||{};let rows=Object.entries(st).map(([k,v])=>v.label+': '+v.avg+'s / بیشینه '+v.max+'s (از '+v.n+' سیگنال)');
let big=x.live&&x.live.latency_biggest? '\n⏳ گلوگاه: '+st[x.live.latency_biggest].label:'';
document.querySelector('#latency').textContent=rows.length?rows.join('\n')+big:'هنوز سیگنالی ارسال نشده';
document.querySelector('#mode').textContent=x.dry_run?'⚠ حالت آزمایشی (ارسال به کانال خاموش)':''}
async function startMeeting(){let url=document.querySelector('#url').value;if(!url)return alert('لینک جلسه را وارد کنید');let x=await api('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});document.querySelector('#result').textContent=JSON.stringify(x,null,2);refresh()}
async function endMeeting(){let x=await api('/api/end',{method:'POST'});document.querySelector('#result').textContent=JSON.stringify(x,null,2);refresh()}
async function simulate(){let x=await api('/api/simulate',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});document.querySelector('#result').textContent=JSON.stringify(x,null,2)}
async function loadDevices(){try{let x=await api('/api/audio-devices');let s=document.querySelector('#adev');let opts=['<option value="">خودکار (بلندگوی پیش‌فرض)</option>'];
for(const d of (x.loopback||[]))opts.push('<option value="'+d+'">'+d+' — loopback</option>');
for(const d of (x.inputs||[]))opts.push('<option value="'+d+'">'+d+'</option>');s.innerHTML=opts.join('')}catch(e){}}
async function setDevice(){let d=document.querySelector('#adev').value||null;let x=await api('/api/audio-device',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device:d})});document.querySelector('#adevmsg').textContent=x.message||('خطا: '+(x.error||''))}
refresh();loadDevices();setInterval(refresh,5000)</script></html>"""


class Dashboard:
    def __init__(self, orchestrator: Any, cfg: dict[str, Any]):
        self.orchestrator = orchestrator
        d = cfg.get("dashboard", {})
        self.host = d.get("host", "127.0.0.1")
        self.port = int(d.get("port", 8765))
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def send_json(self, value: dict[str, Any], code: int = 200):
                data = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path == "/":
                    data = HTML.encode()
                    self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
                if self.path == "/api/status":
                    self.send_json(owner.orchestrator.status()); return
                if self.path == "/api/audio-devices":
                    self.send_json(owner.orchestrator.audio_devices()); return
                self.send_json({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw or b"{}")
                try:
                    if path == "/api/start":
                        result = asyncio.run_coroutine_threadsafe(owner.orchestrator.start_manual(body.get("url", "")), owner.orchestrator.loop).result(60)
                        self.send_json({"ok": True, "message": result}); return
                    if path == "/api/end":
                        asyncio.run_coroutine_threadsafe(owner.orchestrator.end_meeting(), owner.orchestrator.loop).result(60)
                        self.send_json({"ok": True}); return
                    if path == "/api/audio-device":
                        device = body.get("device")
                        msg = asyncio.run_coroutine_threadsafe(
                            owner.orchestrator.set_audio_device(device), owner.orchestrator.loop).result(60)
                        self.send_json({"ok": True, "message": msg}); return
                    if path == "/api/simulate":
                        from app.simulation import run_simulation
                        result = asyncio.run(run_simulation())
                        self.send_json({"ok": True, "result": result}); return
                except Exception as e:  # noqa: BLE001
                    self.send_json({"ok": False, "error": str(e)}, 500); return
                self.send_json({"error": "not found"}, 404)

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

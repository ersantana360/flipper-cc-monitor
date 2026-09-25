#!/usr/bin/env python3
"""Claude Code <-> Flipper Zero bridge.

Hooks push session state here; the Flipper polls it and answers allow/deny.

  GET  /state?client=flipper   latest state (the Flipper polls this ~1/s)
  POST /state  {"status","detail"}   hooks push a state update
  POST /ask    {"tool","detail"}     PreToolUse: blocks until the Flipper answers
  POST /decision  allow | deny       Flipper button press
  GET  /health                       debugging

If no Flipper has polled in the last few seconds, /ask answers "passthrough"
immediately so a session never hangs on a device that is not running the app.
Set CC_GATE_STRICT=1 to deny instead.

USB alerts: when the Flipper is plugged into the PC by USB, the bridge also buzzes and lights it
through tools/flip_alert.py (Windows Python + serial CLI): double vibration + red LED while a
decision is pending, blue when Claude needs you, green blink when a session finishes.
Off by default; start the bridge with CC_USB_ALERTS=1 to enable.

Cloud relay: with CC_CLOUD_URL (+ CC_CLOUD_TOKEN) set, the bridge mirrors its state to the
Cloudflare Worker in cloud/ and collects decisions from it, so the Flipper can poll the Worker
from anywhere and no inbound port is needed on the PC. GET /state?wait=<ms> long-polls (max 3 s).
"""
import json, os, queue, shutil, subprocess, sys, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("CC_BRIDGE_PORT", "8730"))
ASK_TIMEOUT = float(os.environ.get("CC_ASK_TIMEOUT", "90"))   # seconds before a walk-away denies
FLIPPER_STALE = float(os.environ.get("CC_FLIPPER_STALE", "6"))  # no poll for this long => Flipper absent
STRICT = os.environ.get("CC_GATE_STRICT") == "1"
DETAIL_MAX = 60   # the 128x64 screen shows ~21 chars per line; we scroll/wrap on the device

cond = threading.Condition()
ask_serial = threading.Lock()          # one pending decision at a time
state = {"status": "idle", "detail": ""}
pending = None                          # {"id", "tool", "detail", "answer"}
ask_seq = 0
last_flipper_poll = 0.0
version = 1                             # bumps on every state change (long-poll + cloud sync)
MAX_WAIT = 3.0

CLOUD_URL = os.environ.get("CC_CLOUD_URL", "").rstrip("/")
CLOUD_TOKEN = os.environ.get("CC_CLOUD_TOKEN", "")
cloud_flipper_seen = 0.0                # last time the cloud reported the Flipper polling
cloud_dirty = threading.Event()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def clean(text, limit=DETAIL_MAX):
    """Make text safe for the tiny JSON parser on the Flipper and for its font."""
    text = " ".join(str(text or "").split())
    text = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else ("'" if c == '"' else "/" if c == "\\" else "?")
                   for c in text)
    return text[:limit]


def flipper_online():
    now = time.time()
    return now - last_flipper_poll < FLIPPER_STALE or now - cloud_flipper_seen < FLIPPER_STALE


def bump():
    """Call with cond held after changing state/pending."""
    global version
    version += 1
    cond.notify_all()
    cloud_dirty.set()


# ---------- cloud relay sync (optional) ----------
def cloud_request(method, path, body=None):
    if not CLOUD_URL:
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CLOUD_URL + path, data=data, method=method,
                                 headers={"X-Token": CLOUD_TOKEN, "Content-Type": "application/json",
                                          "User-Agent": "cc-bridge/1.0"})  # Cloudflare blocks the default urllib UA
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, ValueError, OSError) as e:
        log(f"cloud {method} {path} failed: {e}")
        return None


def cloud_push():
    """Push the current state; learn whether a Flipper is polling the cloud."""
    global cloud_flipper_seen
    with cond:
        body = {"status": state["status"], "detail": state["detail"],
                "pending": "1" if pending else "0", "ask": str(pending["id"]) if pending else "0"}
    resp = cloud_request("PUT", "/state", body)
    if resp and resp.get("flipper_online"):
        cloud_flipper_seen = time.time()
    return resp is not None


def cloud_pusher():
    while True:
        cloud_dirty.wait()
        cloud_dirty.clear()
        time.sleep(0.2)      # coalesce bursts
        cloud_dirty.clear()
        cloud_push()


def cloud_take_decision():
    """Collect a decision the Flipper left at the cloud; returns 'allow'/'deny' or None."""
    resp = cloud_request("GET", "/decision?take=1")
    if resp and resp.get("answer") in ("allow", "deny"):
        return resp["answer"]
    return None


# ---------- USB alerts (vibration / LED over the Flipper's serial CLI) ----------
ALERT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "flip_alert.py")
alert_q = queue.Queue()
last_alert = None


def _alert_command():
    py = shutil.which("py.exe") or "/mnt/c/Windows/py.exe"
    if os.environ.get("CC_USB_ALERTS", "0") != "1" or not os.path.exists(py):  # opt-in
        return None
    script = ALERT_SCRIPT
    if shutil.which("wslpath"):  # running inside WSL: hand Windows a UNC path
        try:
            script = subprocess.check_output(["wslpath", "-w", ALERT_SCRIPT], text=True).strip()
        except Exception:
            pass
    return [py, "-3", script]


ALERT_CMD = _alert_command()


def alert(kind):
    """Queue an alert; 'working' only clears a lingering 'notify' light."""
    global last_alert
    if not ALERT_CMD:
        return
    if kind == "working" and last_alert != "notify":
        return
    last_alert = kind
    alert_q.put(kind)


def alert_worker():
    while True:
        kind = alert_q.get()
        while not alert_q.empty():  # bursts: only the latest matters
            kind = alert_q.get()
        try:
            subprocess.run(ALERT_CMD + [kind], timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"usb alert -> {kind}")
        except Exception as e:
            log(f"usb alert {kind} failed: {e}")


class Bridge(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def read_json(self):
        raw = self.read_body()
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        global last_flipper_poll
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/state":
            wait = min(float(q.get("wait", ["0"])[0] or 0) / 1000.0, MAX_WAIT)
            with cond:
                if q.get("client", [""])[0] == "flipper":
                    last_flipper_poll = time.time()
                if wait > 0:
                    v = version
                    deadline = time.time() + wait
                    while version == v and time.time() < deadline:
                        cond.wait(timeout=deadline - time.time())
                out = {
                    "status": state["status"],
                    "detail": state["detail"],
                    "pending": "1" if pending else "0",
                    "ask": str(pending["id"]) if pending else "0",
                    "v": str(version),
                }
            self.reply(200, json.dumps(out, separators=(",", ":")).encode(), "application/json")
        elif url.path == "/health":
            with cond:
                out = {"flipper_online": flipper_online(), "pending": pending, "state": state,
                       "strict": STRICT, "ask_timeout": ASK_TIMEOUT, "cloud": CLOUD_URL or None}
            self.reply(200, json.dumps(out).encode(), "application/json")
        elif url.path == "/decision":  # GET form used by the Flipper app
            self.handle_decision(q.get("answer", [""])[0])
        elif url.path == "/ask":  # GET form kept for curl testing
            self.handle_ask({"tool": q.get("tool", ["tool"])[0], "detail": q.get("detail", [""])[0]})
        else:
            self.reply(404)

    def do_POST(self):
        global last_flipper_poll
        url = urlparse(self.path)
        if url.path == "/state":
            data = self.read_json()
            with cond:
                if "status" in data:
                    state["status"] = clean(data["status"], 16) or "idle"
                if "detail" in data:
                    state["detail"] = clean(data["detail"])
                bump()
            log(f"state <- {state['status']}: {state['detail']}")
            if state["status"] == "waiting":
                alert("notify")
            elif state["status"] == "idle":
                alert("done")
            elif state["status"] == "working":
                alert("working")
            self.reply(204)
        elif url.path == "/ask":
            self.handle_ask(self.read_json())
        elif url.path == "/decision":
            self.handle_decision(self.read_body().decode("utf-8", "replace"))
        else:
            self.reply(404)

    def handle_decision(self, raw):
        global last_flipper_poll
        answer = raw.strip().strip('"{} ').lower()
        if ":" in answer:  # {"decision":"allow"}
            answer = answer.split(":")[-1].strip('" ')
        with cond:
            last_flipper_poll = time.time()
            if pending and pending["answer"] is None and answer in ("allow", "deny"):
                pending["answer"] = answer
                bump()
                log(f"decision <- {answer} (ask #{pending['id']})")
            else:
                log(f"decision <- {answer!r} ignored (nothing pending)")
        self.reply(200, b"ok")

    def handle_ask(self, data):
        global pending, ask_seq
        tool = clean(data.get("tool", "tool"), 16) or "tool"
        detail = clean(data.get("detail", ""))
        with ask_serial:                       # queue concurrent asks, one at a time
            if CLOUD_URL:
                cloud_push()                   # fresh answer to "is a Flipper polling the cloud?"
            with cond:
                if not flipper_online() and not STRICT:
                    log(f"ask {tool} {detail!r} -> passthrough (no Flipper polling)")
                    state.update(status="working", detail=detail or tool)
                    bump()
                    self.reply(200, b"passthrough")
                    return
                ask_seq += 1
                pending = {"id": ask_seq, "tool": tool, "detail": detail, "answer": None}
                state.update(status="waiting", detail=f"{tool}: {detail}" if detail else tool)
                bump()
                log(f"ask #{ask_seq} {tool} {detail!r} -> waiting for the Flipper")
                alert("pending")
                deadline = time.time() + ASK_TIMEOUT
                while pending["answer"] is None and time.time() < deadline:
                    cond.wait(timeout=1.0)
                    if pending["answer"] is None and CLOUD_URL:
                        cond.release()
                        try:
                            got = cloud_take_decision()
                        finally:
                            cond.acquire()
                        if got and pending and pending["answer"] is None:
                            pending["answer"] = got
                            log(f"decision <- {got} (ask #{pending['id']}, via cloud)")
                answer = pending["answer"] or "deny"
                if pending["answer"] is None:
                    log(f"ask #{pending['id']} timed out -> deny")
                pending = None
                state.update(status="working" if answer == "allow" else "denied",
                             detail=detail or tool)
                bump()
                alert(answer)
        self.reply(200, answer.encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    log(f"bridge on :{PORT}  (ask timeout {ASK_TIMEOUT:.0f}s, strict={STRICT}, usb alerts={'on' if ALERT_CMD else 'off'})")
    if ALERT_CMD:
        threading.Thread(target=alert_worker, daemon=True).start()
    if CLOUD_URL:
        log(f"cloud relay: {CLOUD_URL} ({'token set' if CLOUD_TOKEN else 'NO TOKEN'})")
        threading.Thread(target=cloud_pusher, daemon=True).start()
        cloud_dirty.set()
    try:
        ThreadingHTTPServer(("", PORT), Bridge).serve_forever()
    except KeyboardInterrupt:
        pass

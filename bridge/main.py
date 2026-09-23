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
"""
import json, os, sys, threading, time
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


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def clean(text, limit=DETAIL_MAX):
    """Make text safe for the tiny JSON parser on the Flipper and for its font."""
    text = " ".join(str(text or "").split())
    text = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else ("'" if c == '"' else "/" if c == "\\" else "?")
                   for c in text)
    return text[:limit]


def flipper_online():
    return time.time() - last_flipper_poll < FLIPPER_STALE


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
            with cond:
                if q.get("client", [""])[0] == "flipper":
                    last_flipper_poll = time.time()
                out = {
                    "status": state["status"],
                    "detail": state["detail"],
                    "pending": "1" if pending else "0",
                    "ask": str(pending["id"]) if pending else "0",
                }
            self.reply(200, json.dumps(out, separators=(",", ":")).encode(), "application/json")
        elif url.path == "/health":
            with cond:
                out = {"flipper_online": flipper_online(), "pending": pending, "state": state,
                       "strict": STRICT, "ask_timeout": ASK_TIMEOUT}
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
            log(f"state <- {state['status']}: {state['detail']}")
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
                cond.notify_all()
                log(f"decision <- {answer} (ask #{pending['id']})")
            else:
                log(f"decision <- {answer!r} ignored (nothing pending)")
        self.reply(200, b"ok")

    def handle_ask(self, data):
        global pending, ask_seq
        tool = clean(data.get("tool", "tool"), 16) or "tool"
        detail = clean(data.get("detail", ""))
        with ask_serial:                       # queue concurrent asks, one at a time
            with cond:
                if not flipper_online() and not STRICT:
                    log(f"ask {tool} {detail!r} -> passthrough (no Flipper polling)")
                    state.update(status="working", detail=detail or tool)
                    self.reply(200, b"passthrough")
                    return
                ask_seq += 1
                pending = {"id": ask_seq, "tool": tool, "detail": detail, "answer": None}
                state.update(status="waiting", detail=f"{tool}: {detail}" if detail else tool)
                log(f"ask #{ask_seq} {tool} {detail!r} -> waiting for the Flipper")
                deadline = time.time() + ASK_TIMEOUT
                while pending["answer"] is None and time.time() < deadline:
                    cond.wait(timeout=1.0)
                answer = pending["answer"] or "deny"
                if pending["answer"] is None:
                    log(f"ask #{pending['id']} timed out -> deny")
                pending = None
                state.update(status="working" if answer == "allow" else "denied",
                             detail=detail or tool)
        self.reply(200, answer.encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    log(f"bridge on :{PORT}  (ask timeout {ASK_TIMEOUT:.0f}s, strict={STRICT})")
    try:
        ThreadingHTTPServer(("", PORT), Bridge).serve_forever()
    except KeyboardInterrupt:
        pass

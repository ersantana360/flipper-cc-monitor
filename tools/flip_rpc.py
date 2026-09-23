"""Minimal Flipper Zero RPC client over USB serial (protobuf): screenshots, key presses, app start.
Usage:
  flip_rpc.py COM3 shot [out.png]
  flip_rpc.py COM3 seq "<step>[,<step>...]"   steps: ok|back|up|down|left|right (short), +ok (long), app:/ext/apps/GPIO/x.fap, wait:1.5, shot, shot:name
"""
import os, struct, sys, time, zlib, serial
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pb"))
import flipper_pb2 as PB, gui_pb2 as GUI, application_pb2 as APPP

KEYS = {"up": GUI.UP, "down": GUI.DOWN, "right": GUI.RIGHT, "left": GUI.LEFT, "ok": GUI.OK, "back": GUI.BACK}
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

def venc(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        if n: out += bytes([b | 0x80])
        else: return out + bytes([b])

class Rpc:
    def __init__(self, port):
        self.s = serial.Serial(port, 230400, timeout=0.5)
        self.cid = 0
        self.s.write(b"\r"); time.sleep(0.3); self.s.reset_input_buffer()
        self.s.write(b"start_rpc_session\r")
        # wait for the echo + newline, then protobuf mode
        buf = b""; t = time.time()
        while time.time() - t < 3:
            c = self.s.read(1)
            if c:
                buf += c
                if buf.endswith(b"start_rpc_session\r\n"): break
        self.frame = None
    def send(self, msg):
        self.cid += 1
        msg.command_id = self.cid
        data = msg.SerializeToString()
        self.s.write(venc(len(data)) + data)
        return self.cid
    def read_varint(self, timeout):
        n = 0; shift = 0; t = time.time()
        while time.time() - t < timeout:
            c = self.s.read(1)
            if not c: continue
            b = c[0]; n |= (b & 0x7F) << shift; shift += 7
            if not (b & 0x80): return n
        return None
    def recv(self, timeout=3.0):
        n = self.read_varint(timeout)
        if n is None: return None
        data = b""; t = time.time()
        while len(data) < n and time.time() - t < timeout:
            data += self.s.read(n - len(data))
        m = PB.Main(); m.ParseFromString(data); return m
    def wait_status(self, cid, timeout=5.0):
        t = time.time()
        while time.time() - t < timeout:
            m = self.recv(timeout)
            if m is None: continue
            if m.HasField("gui_screen_frame"): self.frame = bytes(m.gui_screen_frame.data)
            if m.command_id == cid: return m
        return None
    def screenshot(self, name="screen"):
        m = PB.Main(); m.gui_start_screen_stream_request.SetInParent()
        cid = self.send(m); self.frame = None
        t = time.time()
        while time.time() - t < 4:
            r = self.recv(2.0)
            if r is None: continue
            if r.HasField("gui_screen_frame"):
                self.frame = bytes(r.gui_screen_frame.data); break
        m = PB.Main(); m.gui_stop_screen_stream_request.SetInParent(); cid = self.send(m); self.wait_status(cid, 2)
        time.sleep(0.2); self.s.reset_input_buffer()
        if self.frame is None: print("no frame received"); return None
        return save_png(self.frame, os.path.join(OUT_DIR, f"{name}.png"))
    def key(self, key, long=False):
        for t in ([GUI.PRESS, GUI.LONG, GUI.RELEASE] if long else [GUI.PRESS, GUI.SHORT, GUI.RELEASE]):
            m = PB.Main(); m.gui_send_input_event_request.key = KEYS[key]; m.gui_send_input_event_request.type = t
            cid = self.send(m); self.wait_status(cid, 2)
            time.sleep(0.35 if t == GUI.LONG else 0.05)
        time.sleep(0.3)
    def app_start(self, path, args=""):
        m = PB.Main(); m.app_start_request.name = path; m.app_start_request.args = args
        cid = self.send(m); r = self.wait_status(cid, 5)
        print("app_start:", PB.CommandStatus.Name(r.command_status) if r else "no reply")
    def close(self):
        m = PB.Main(); m.stop_session.SetInParent(); self.send(m)
        time.sleep(0.3); self.s.close()

def decode(frame):
    px = [[0] * 128 for _ in range(64)]
    for i, b in enumerate(frame[:1024]):
        x = i % 128; page = i // 128
        for bit in range(8):
            px[page * 8 + bit][x] = (b >> bit) & 1
    return px

def save_png(frame, path, scale=3):
    px = decode(frame)
    w, h = 128 * scale, 64 * scale
    raw = b""
    for y in range(h):
        row = bytes(0 if px[y // scale][x // scale] else 255 for x in range(w))
        raw += b"\x00" + row
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    open(path, "wb").write(png)
    # ascii preview (2x1 downsample horizontally)
    lines = []
    for y in range(0, 64, 2):
        lines.append("".join("#" if (px[y][x] or px[y + 1][x]) else "." for x in range(128)))
    print("\n".join(lines))
    print("saved", path)
    return path

if __name__ == "__main__":
    port, cmd = sys.argv[1], sys.argv[2]
    r = Rpc(port)
    try:
        if cmd == "shot":
            r.screenshot(sys.argv[3] if len(sys.argv) > 3 else "screen")
        elif cmd == "seq":
            for step in sys.argv[3].split(","):
                step = step.strip()
                if not step: continue
                if step == "shot": r.screenshot()
                elif step.startswith("shot:"): r.screenshot(step[5:])
                elif step.startswith("app:"): r.app_start(step[4:]); time.sleep(1.5)
                elif step.startswith("wait:"): time.sleep(float(step[5:]))
                elif step.startswith("+"): r.key(step[1:], long=True); print("long", step[1:])
                elif step in KEYS: r.key(step); print("key", step)
                else: print("unknown step", step)
    finally:
        r.close()

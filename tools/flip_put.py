"""Upload files to the Flipper Zero SD card over the USB CLI (binary-safe via storage write_chunk).
Usage: flip_put.py COM3 local1:remote1 [local2:remote2 ...]"""
import hashlib, os, sys, time, serial

PROMPT = b">: "

class Flip:
    def __init__(self, port):
        self.s = serial.Serial(port, 230400, timeout=2)
        self.s.write(b"\r"); time.sleep(0.5); self.s.reset_input_buffer()
    def read_until(self, marker, timeout=5):
        buf = b""; t = time.time()
        while time.time() - t < timeout:
            c = self.s.read(1)
            if c:
                buf += c
                if buf.endswith(marker): break
        return buf
    def cmd(self, line, timeout=10):
        self.s.reset_input_buffer()
        self.s.write(line.encode() + b"\r")
        out = self.read_until(PROMPT, timeout).decode("utf-8", "replace")
        return out
    def put(self, local, remote, chunk=4096):
        size = os.path.getsize(local)
        self.cmd(f'storage remove "{remote}"', 5)
        sent = 0; t0 = time.time()
        with open(local, "rb") as f:
            while True:
                data = f.read(chunk)
                if not data: break
                self.s.reset_input_buffer()
                self.s.write(f'storage write_chunk "{remote}" {len(data)}\r'.encode())
                echo = self.read_until(b"\n", 5)          # command echo
                ready = self.read_until(b"\n", 5)         # "Ready?" or error
                if b"Ready" not in ready:
                    raise RuntimeError(f"write_chunk refused: {echo!r} {ready!r}")
                self.s.write(data)
                tail = self.read_until(PROMPT, 30)
                if b"error" in tail.lower() or b"Storage error" in tail:
                    raise RuntimeError(f"write error: {tail!r}")
                sent += len(data)
        dt = time.time() - t0
        md5_local = hashlib.md5(open(local, "rb").read()).hexdigest()
        out = self.cmd(f'storage md5 "{remote}"', 60)
        md5_remote = [l.strip() for l in out.splitlines() if len(l.strip()) == 32]
        ok = md5_remote and md5_remote[0] == md5_local
        print(f"{'OK ' if ok else 'BAD'} {remote}  {sent} bytes in {dt:.1f}s  md5 local={md5_local} remote={md5_remote}")
        return ok

if __name__ == "__main__":
    fl = Flip(sys.argv[1])
    for d in ["/ext/apps_data/esp_flasher", "/ext/apps_data/cc_monitor", "/ext/apps/GPIO"]:
        fl.cmd(f'storage mkdir "{d}"')
    all_ok = True
    for pair in sys.argv[2:]:
        local, remote = pair.split(":", 1)
        all_ok &= bool(fl.put(local, remote))
    print("ALL OK" if all_ok else "SOME FAILED")

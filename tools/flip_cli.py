import serial, sys, time
port = sys.argv[1]; cmds = sys.argv[2:]
s = serial.Serial(port, 230400, timeout=1)
def rd(timeout=2.5):
    buf = b""; t = time.time()
    while time.time() - t < timeout:
        chunk = s.read(4096)
        if chunk:
            buf += chunk; t = time.time() if not buf.endswith(b">: ") else t
            if buf.endswith(b">: "): break
    return buf.decode("utf-8", "replace")
s.write(b"\r\n"); time.sleep(0.3); rd(1.0)
for c in cmds:
    s.write(c.encode() + b"\r\n")
    print("$ " + c); print(rd(4.0)); print("-----")
s.close()

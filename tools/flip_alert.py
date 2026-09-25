"""Buzz / light the Flipper Zero over its USB CLI (runs with the Windows Python).

Usage: flip_alert.py <pending|allow|deny|notify|done|working|clear> [COMx]
Exits quietly if the Flipper is not plugged in or the port is busy (qFlipper, lab.flipper.net, other tools).
"""
import sys, time
import serial
from serial.tools import list_ports

FLIPPER_VID, FLIPPER_PID = 0x0483, 0x5740

# (cli command, delay after it in seconds)
PATTERNS = {
    "pending": [("vibro 1", 0.15), ("vibro 0", 0.10), ("vibro 1", 0.15), ("vibro 0", 0), ("led r 255", 0)],  # steady red until answered
    "allow":   [("led r 0", 0), ("led g 255", 0.4), ("led g 0", 0)],
    "deny":    [("led r 0", 0.15), ("led r 255", 0.2), ("led r 0", 0.15), ("led r 255", 0.2), ("led r 0", 0)],
    "notify":  [("vibro 1", 0.2), ("vibro 0", 0), ("led b 255", 0)],                                       # steady blue: Claude needs you
    "done":    [("led b 0", 0), ("led r 0", 0), ("led g 255", 0.6), ("led g 0", 0)],
    "working": [("led b 0", 0), ("led r 0", 0), ("led g 0", 0)],
    "clear":   [("vibro 0", 0), ("led r 0", 0), ("led g 0", 0), ("led b 0", 0)],
}


def find_port():
    for p in list_ports.comports():
        if p.vid == FLIPPER_VID and p.pid == FLIPPER_PID:
            return p.device
    return None


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "clear"
    port = sys.argv[2] if len(sys.argv) > 2 else find_port()
    if kind not in PATTERNS:
        print(f"unknown alert {kind!r}; one of {', '.join(PATTERNS)}", file=sys.stderr)
        return 2
    if not port:
        print("no Flipper on USB", file=sys.stderr)
        return 0
    try:
        s = serial.Serial(port, 230400, timeout=0.3)
    except serial.SerialException as e:
        print(f"port busy: {e}", file=sys.stderr)
        return 0
    try:
        s.write(b"\r"); time.sleep(0.15); s.reset_input_buffer()
        for cmd, delay in PATTERNS[kind]:
            s.write(cmd.encode() + b"\r")
            time.sleep(0.08)
            s.reset_input_buffer()
            if delay:
                time.sleep(delay)
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

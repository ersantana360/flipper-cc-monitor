# flipper-cc-monitor

Watch and gate Claude Code sessions from a Flipper Zero + WiFi dev board.

```
Claude Code hooks ──> bridge (WSL, :8730) <── polls ── WiFi dev board (FlipperHTTP fw) <── UART ── Flipper app
   PreToolUse(Bash) waits on /ask            OK = allow / BACK = deny  ──────────────────────────────>
```

## Layout
- `bridge/main.py` – stdlib-only HTTP hub. `bridge/start.sh` starts it in the background (log: `~/.claude/cc-monitor/bridge.log`).
  It is not auto-started; run `bridge/start.sh` after a reboot (or add it to `~/.bashrc`).
- `hooks/cc-state.sh`, `hooks/cc-ask.sh` – Claude Code hooks (symlinked as `~/.claude/cc-state.sh` / `cc-ask.sh`,
  registered in `~/.claude/settings.json`: PreToolUse[Bash] → cc-ask, PostToolUse/UserPromptSubmit → working, Notification → waiting, Stop → idle).
  Backup of the previous settings: `~/.claude/settings.json.bak-before-cc-monitor`.
- `app/` – the Flipper app (`ufbt` project, FlipperHTTP C library vendored in `app/flipper_http/`). Build with `cd app && ufbt` (SDK pinned to firmware 1.4.3 / API 87.1).
- `firmware/` – FlipperHTTP v2.2.0 binaries for the WiFi dev board (ESP32-S2). The ESP Flasher and FlipperHTTP companion FAPs are downloaded separately (see *Third-party*).
- `tools/` – Windows-side serial tools (upload files, screenshots, remote key presses) and the port-forward script.

## Bridge API
| Route | Who | What |
|---|---|---|
| `GET /state?client=flipper` | Flipper app, ~1/s | `{"status","detail","pending","ask"}` |
| `POST /state` `{"status","detail"}` | hooks | update the screen |
| `POST /ask` `{"tool","detail"}` | PreToolUse hook | blocks until `allow`/`deny`; `deny` after 90 s; `passthrough` immediately when no Flipper has polled in the last 6 s |
| `GET /decision?answer=allow\|deny` or `POST /decision` | Flipper (or curl) | answers the pending ask |
| `GET /health` | you | debugging |

Env knobs for `main.py`: `CC_BRIDGE_PORT`, `CC_ASK_TIMEOUT` (90), `CC_FLIPPER_STALE` (6), `CC_GATE_STRICT=1` (deny instead of passthrough when the Flipper is absent).

**Fail-open by design:** if the bridge is down or the Flipper app is not running, `cc-ask.sh` exits 0 and Claude Code behaves normally.
Close the app on the Flipper (hold BACK) to switch the gate off; open it to switch it on.

## Flipper app
Apps → GPIO → **Claude Code Monitor**. The header shows the link state (`NO BOARD` / `no bridge` / `online`), the body shows the
status word plus the tool/command, and a black bar `OK = ALLOW  BACK = DENY` appears (with a double vibration) when a decision is pending.
Hold BACK to exit.

Config files on the SD card (`apps_data/cc_monitor/`):
- `bridge.txt` – bridge URL, created on first run with `http://192.168.0.9:8730`. Edit if the PC's LAN IP changes.
- `wifi.txt` – optional, line 1 = SSID, line 2 = password (2.4 GHz network). Pushed to the board with `[WIFI/SAVE]` every time the app starts.
  Alternative: set the credentials once with the **FlipperHTTP** companion app (Apps → GPIO → FlipperHTTP).

## Networking on WSL2
WSL2 sits behind NAT, so the board (on the LAN) cannot reach the bridge until Windows forwards the port. Run once as Administrator:
```
powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\ersantana\Development\projects\flipper-cc-monitor\tools\setup-windows-portproxy.ps1
```
It adds a firewall rule for TCP 8730 and a `netsh portproxy` from `0.0.0.0:8730` to WSL (tries `127.0.0.1` first, then the WSL IP), then self-tests `http://<LAN IP>:8730/health`.

## Re-flashing / restoring the dev board
Everything was flashed from the Flipper with ESP Flasher (Apps → GPIO): `Reset Board` → `Enter Bootloader` → `Flash ESP` →
Bootloader (0x1000) = `flipper_http_bootloader.bin`, Part Table (0x8000) = `flipper_http_partitions.bin`, FirmwareA (0x10000) = `flipper_http_firmware_a.bin` → `[>] FLASH - fast`.
The stock debugger firmware can be restored the same way from https://github.com/flipperdevices/blackmagic-esp32-s2 releases.

## Manual test without hardware
```
bridge/start.sh
curl -s "localhost:8730/state?client=flipper"                                  # pretend the Flipper is polling
echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | hooks/cc-ask.sh &   # blocks
curl -s "localhost:8730/decision?answer=allow"                                  # releases it (exit 0); deny -> exit 2
```

## Third-party
- [FlipperHTTP](https://github.com/jblanked/FlipperHTTP) by JBlanked, MIT – the dev-board firmware in `firmware/` and the C library vendored in `app/flipper_http/`.
- [ESP Flasher](https://github.com/0xchocolate/flipperzero-esp-flasher) by 0xchocolate, GPL-3.0 – not bundled; install it from the app catalog (`lab.flipper.net/apps/esp_flasher`) or build it with `ufbt`.
- [FlipperHTTP-App](https://github.com/jblanked/FlipperHTTP-App) by JBlanked – not bundled; install it from the catalog (`lab.flipper.net/apps/flipper_http`). Only needed if you prefer entering WiFi credentials on the Flipper instead of `wifi.txt`.
- `tools/pb/` is generated from [flipperzero-protobuf](https://github.com/flipperdevices/flipperzero-protobuf) with `tools/gen_pb.sh`.

## License
MIT, see `LICENSE`.

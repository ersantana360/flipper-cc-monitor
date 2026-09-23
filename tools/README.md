# Tools (run from WSL; they use the Windows Python because the Flipper's USB port lives on the Windows side)

The Flipper enumerates as a COM port on Windows (COM3 at the time of writing). WSL2 has no USB access unless
usbipd-win is installed, so these scripts are run with `py.exe` and talk to `COMx` directly.

```bash
cd tools
py.exe -3 flip_cli.py COM3 "device_info" "storage list /ext/apps/GPIO"                 # plain CLI commands
py.exe -3 flip_put.py COM3 "../app/dist/cc_monitor.fap:/ext/apps/GPIO/cc_monitor.fap"  # binary-safe upload + md5 check
py.exe -3 flip_rpc.py COM3 shot                                                         # screenshot -> tools/screen.png
py.exe -3 flip_rpc.py COM3 seq "app:/ext/apps/GPIO/cc_monitor.fap,wait:5,shot,+back"   # start app, screenshot, long-BACK
```

`flip_rpc.py` needs `pb/*_pb2.py` and `pyserial` in the Windows Python: run `./gen_pb.sh` once (it installs
`grpcio-tools` + `pyserial` and compiles flipperdevices/flipperzero-protobuf into `pb/`).

`setup-windows-portproxy.ps1` must be run once as Administrator on Windows: it forwards LAN port 8730 into WSL2 and
opens the firewall so the dev board can reach the bridge.

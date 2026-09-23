# Run ONCE as Administrator on the Windows host (right-click PowerShell -> Run as administrator):
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\ersantana\Development\projects\flipper-cc-monitor\tools\setup-windows-portproxy.ps1
# Forwards LAN port 8730 into WSL2 and opens the Windows firewall, so the WiFi dev board
# (on your LAN) can reach the bridge that runs inside WSL. Re-run after a reboot if the WSL IP changed
# (the script tries 127.0.0.1 first, which is stable across reboots).
param([string]$WslIp = "", [int]$Port = 8730)
if (-not $WslIp) { $WslIp = (wsl.exe hostname -I).Trim().Split(" ")[0] }
$lanIp = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -eq "Ethernet" -or $_.InterfaceAlias -like "Wi-Fi*" } | Select-Object -First 1).IPAddress
Write-Host "WSL IP: $WslIp   LAN IP: $lanIp"
if (-not (Get-NetFirewallRule -DisplayName "CC Flipper Bridge $Port" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "CC Flipper Bridge $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Any | Out-Null
  Write-Host "firewall rule created"
}
function Test-Bridge { try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://$lanIp`:$Port/health").StatusCode -eq 200 } catch { $false } }
foreach ($target in @("127.0.0.1", $WslIp)) {
  netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 | Out-Null
  netsh interface portproxy add v4tov4 listenport=$Port listenaddress=0.0.0.0 connectport=$Port connectaddress=$target | Out-Null
  Start-Sleep -Seconds 1
  $ok = Test-Bridge
  Write-Host "portproxy 0.0.0.0:$Port -> $target : $(if ($ok) {'OK, bridge reachable at http://' + $lanIp + ':' + $Port} else {'no answer (is the bridge running in WSL?)'})"
  if ($ok) { break }
}
netsh interface portproxy show v4tov4

<#
  NETRA-X collector -- one-command install for Windows.

  Run in an administrator PowerShell:

    irm https://raw.githubusercontent.com/kevivek-cyber/NETRA-X-Collector/main/install.ps1 | iex

  Or with the token, so nothing is asked:

    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/kevivek-cyber/NETRA-X-Collector/main/install.ps1))) -Token <token>

  Downloads the portable build (Python, libraries and Tor included), unpacks
  it to C:\NETRA-X-Collector, saves the token, adds a desktop shortcut and
  starts the collector. Running it again updates in place.
#>
param(
    [string]$Token = $env:NETRAX_COLLECTOR_TOKEN,
    [string]$Dir = "C:\NETRA-X-Collector",
    [string]$Server = "https://console.onnetra.in"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zipUrl = "https://github.com/kevivek-cyber/NETRA-X-Collector/releases/latest/download/NETRA-X-Collector-win64.zip"

Write-Host ""
Write-Host "  NETRA-X collector setup" -ForegroundColor Cyan
Write-Host "  -----------------------"

# A collector already running from this folder holds its files open.
Get-CimInstance Win32_Process |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$tmp = Join-Path $env:TEMP ("netrax-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host "  Downloading the portable build (about 16 MB)..."
    $zip = Join-Path $tmp "collector.zip"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
    Unblock-File $zip
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    if (Test-Path $Dir) { Remove-Item $Dir -Recurse -Force }
    Move-Item (Join-Path $tmp "NETRA-X-Collector") $Dir
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
Get-ChildItem $Dir -Recurse -File | Unblock-File
Write-Host "  Installed to $Dir"

if (-not $Token) { $Token = [Environment]::GetEnvironmentVariable("NETRAX_COLLECTOR_TOKEN", "User") }
if (-not $Token) { $Token = Read-Host "  Paste the collector token" }
if (-not $Token) { throw "No token given; the collector cannot sign in without one." }
[Environment]::SetEnvironmentVariable("NETRAX_COLLECTOR_TOKEN", $Token.Trim(), "User")
[Environment]::SetEnvironmentVariable("NETRAX_SERVER", $Server, "User")
$env:NETRAX_COLLECTOR_TOKEN = $Token.Trim()
$env:NETRAX_SERVER = $Server
Write-Host "  Token saved for this Windows user"

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop "NETRA-X Collector.lnk"))
$lnk.TargetPath = Join-Path $Dir "Run-Collector.cmd"
$lnk.WorkingDirectory = $Dir
$lnk.IconLocation = (Join-Path $Dir "tor\tor.exe") + ",0"
$lnk.Save()
Write-Host "  Desktop shortcut: NETRA-X Collector"

Start-Process -FilePath (Join-Path $Dir "Run-Collector.cmd") -WorkingDirectory $Dir
Write-Host ""
Write-Host "  Started in a new window. Leave it open; the console shows this PC online in a few seconds." -ForegroundColor Green

# Instructions for an AI agent setting up this collector

Goal: get the NETRA-X collector running on this PC and showing as online.

**Windows, fastest path:** in an administrator PowerShell run
`& ([scriptblock]::Create((irm https://raw.githubusercontent.com/kevivek-cyber/NETRA-X-Collector/main/install.ps1))) -Token <token>`
(ask the human for the token). It installs a portable build with Python and
Tor included and starts the collector; then check step 8 below. Use the
manual steps only if that fails.

1. Detect the OS.
2. Ensure Python 3.10+ is installed (`py -3 --version` / `python3 --version`).
   If missing on Windows: `winget install -e --id Python.Python.3.12`.
3. Ensure Tor is available:
   - Windows: Tor Browser at `%USERPROFILE%\Desktop\Tor Browser\` (or Downloads,
     or `C:\Program Files\Tor Browser\`). If missing:
     `winget install -e --id TorProject.TorBrowser`, then confirm `tor.exe`
     exists under `...\Tor Browser\Browser\TorBrowser\Tor\`; if winget put it
     elsewhere, put the folder containing tor.exe on PATH.
   - Linux: `sudo apt install -y tor`. macOS: `brew install tor`.
4. Create the venv and install requirements:
   `python -m venv .venv` then `.venv` python `-m pip install -r requirements.txt`.
5. Verify the code imports and Tor is found:
   `.venv python -c "from workers.collection.tor_client import find_running_proxy as p, find_tor_binary as b; print(p(), b())"`
   -- at least one value must be non-None.
6. The collector token is a secret. **Ask the human for it**; never invent one,
   never commit it, never print it back. Save it as a user environment
   variable (Windows: `setx NETRAX_COLLECTOR_TOKEN "<token>"`; Linux/macOS:
   export it in the shell profile).
7. Start it: Windows -> run `Start-Collector.cmd` in a new console window and
   leave it open; Linux/macOS -> `./start.sh`.
8. Success = the log shows `collector <hostname> -> https://console.onnetra.in`
   with no `401`/`403` (a 401 means a wrong token) and heartbeats do not fail.
9. Optional (Windows): make a desktop shortcut to `Start-Collector.cmd`.

Do not modify the Python code to get past errors; report them instead.

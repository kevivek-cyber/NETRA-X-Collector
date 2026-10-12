# NETRA-X Collector

The half of NETRA-X that is allowed to touch Tor. The console at
<https://console.onnetra.in> queues crawls; this program, running on any PC
that can reach the Tor network, claims them, fetches the pages over Tor and
posts them back. Every connection is outbound -- no ports to open, works
behind NAT or on a hotspot.

## Windows: one command

Open **PowerShell as administrator** and run:

```powershell
irm https://raw.githubusercontent.com/kevivek-cyber/NETRA-X-Collector/main/install.ps1 | iex
```

It downloads the portable build (Python, libraries and Tor bundled -- nothing
else to install), unpacks it to `C:\NETRA-X-Collector`, asks for the collector
token once, adds a desktop shortcut and starts the collector. Run it again to
update.

## Windows, by hand

1. Install **Python 3.10+** from python.org (tick "Add python.exe to PATH").
2. Install **Tor Browser** to the Desktop (the default location). It does not
   need to be open -- the collector starts its own Tor when a crawl arrives.
3. Download this repo (Code -> Download ZIP, or `git clone`) and double-click
   **`Start-Collector.cmd`**.
4. Paste the collector token when asked. It is saved; you will not be asked again.

Leave the window open. The console shows the collector as online within a few
seconds; closing the window marks it offline.

## Linux / macOS

```sh
sudo apt install tor python3-venv     # or: brew install tor python
export NETRAX_COLLECTOR_TOKEN=...     # from the NETRA-X admin
./start.sh
```

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `NETRAX_COLLECTOR_TOKEN` | -- (required) | Shared secret the server checks |
| `NETRAX_SERVER` | `https://console.onnetra.in` | Control plane URL |
| `NETRAX_COLLECTOR_ID` | the PC's hostname | Name shown in the console |

`--once` handles a single job and exits; `-v` logs more.

## Checking it works

- The window prints `collector <name> -> https://console.onnetra.in` and no errors.
- In the console, open a scan: the amber "No collector is online" banner disappears.
- Start a crawl of a demo site from the scan window; pages appear as they are fetched.

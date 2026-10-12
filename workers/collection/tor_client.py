"""
Tor connectivity for the crawler.

All network access in the collection pipeline goes through this module, so
there is exactly one place where "how do we reach a .onion" is answered.

Two ways to get a circuit, in order:

  1. A SOCKS proxy already listening (Tor Browser on 9150, a system tor on
     9050). Reused as-is -- starting a second Tor beside a running one wastes
     a bootstrap and can collide on the data directory.
  2. Our own tor process, from a private torrc with its own data directory and
     port, so it cannot disturb Tor Browser's state.

A demo that needs someone to remember to open Tor Browser first is a demo that
fails on stage, which is why (2) exists at all.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Optional, Sequence

import httpx

DEFAULT_PROXY_PORTS = (9150, 9050)
OWN_SOCKS_PORT = 9251
BOOTSTRAP_TIMEOUT = 90.0
# Onion sites are slow: a circuit is six hops, and a real hidden service
# answered in 89s during testing. A 30s cap made ordinary sites look dead.
REQUEST_TIMEOUT = 180.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

_BOOTSTRAP_RE = re.compile(r"Bootstrapped (\d{1,3})%")


class TorUnavailable(RuntimeError):
    """No Tor could be found or started."""


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: int = 0


def _port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def find_running_proxy(ports: Sequence[int] = DEFAULT_PROXY_PORTS) -> Optional[int]:
    """The first SOCKS port already answering, or None."""
    for port in ports:
        if _port_is_open(port):
            return port
    return None


def find_tor_binary() -> Optional[Path]:
    """tor from the Tor Browser bundle, a common install, or PATH."""
    candidates = []
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE")
        if profile:
            candidates.append(Path(profile) / "Desktop/Tor Browser/Browser/TorBrowser/Tor/tor.exe")
            candidates.append(Path(profile) / "Downloads/Tor Browser/Browser/TorBrowser/Tor/tor.exe")
        candidates.append(Path("C:/Program Files/Tor Browser/Browser/TorBrowser/Tor/tor.exe"))
    candidates += [Path("/usr/bin/tor"), Path("/usr/local/bin/tor")]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    found = which("tor") or which("tor.exe")
    return Path(found) if found else None


def write_torrc(data_dir: Path, socks_port: int) -> Path:
    """A minimal torrc: a client circuit and nothing else."""
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "torrc"
    path.write_text(
        f"DataDirectory {data_dir}\n"
        f"SocksPort {socks_port}\n"
        "ClientOnly 1\n"
        "AvoidDiskWrites 1\n",
        encoding="utf-8",
    )
    return path


def parse_bootstrap_percent(line: str) -> Optional[int]:
    match = _BOOTSTRAP_RE.search(line or "")
    return int(match.group(1)) if match else None


class TorClient:
    """Owns a circuit for the duration of a crawl."""

    def __init__(self, data_dir: Optional[Path] = None, socks_port: int = OWN_SOCKS_PORT):
        self.data_dir = Path(data_dir) if data_dir else Path.home() / ".netra-x" / "tor"
        self.socks_port = socks_port
        self.owns_process = False
        self.bootstrap_percent = 0
        self._process = None
        self._client = None

    def start(self) -> None:
        running = find_running_proxy()
        if running is not None:
            self.socks_port = running
            self.owns_process = False
            self.bootstrap_percent = 100
        else:
            binary = find_tor_binary()
            if binary is None:
                raise TorUnavailable(
                    "No Tor available: nothing is listening on 127.0.0.1:9150 or :9050, "
                    "and no tor binary was found in the Tor Browser bundle or on PATH. "
                    "Install Tor Browser, or add tor to PATH."
                )
            torrc = write_torrc(self.data_dir, self.socks_port)
            self._process = subprocess.Popen(
                [str(binary), "-f", str(torrc)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._await_bootstrap()
            self.owns_process = True
        self._client = httpx.Client(
            proxy=f"socks5://127.0.0.1:{self.socks_port}",
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "NETRA-X-Collector/0.1"},
        )

    def _await_bootstrap(self) -> None:
        """Wait for Tor to report a working circuit, or give up saying so.

        Read on a worker thread: on Windows a pipe read blocks uninterruptibly,
        so a tor that starts and then says nothing would hang the crawl rather
        than failing it.
        """
        done = threading.Event()

        def read_output():
            for line in self._process.stdout:
                percent = parse_bootstrap_percent(line)
                if percent is not None:
                    self.bootstrap_percent = percent
                    if percent == 100:
                        done.set()
                        return
            done.set()

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        done.wait(BOOTSTRAP_TIMEOUT)

        if self.bootstrap_percent == 100:
            return
        exited = self._process.poll() is not None
        self.stop()
        if exited:
            raise TorUnavailable("Tor exited before it finished starting.")
        raise TorUnavailable(
            f"Tor did not finish starting within {BOOTSTRAP_TIMEOUT:.0f}s "
            f"(reached {self.bootstrap_percent}%)."
        )

    def fetch(self, url: str) -> FetchResult:
        if self._client is None:
            raise TorUnavailable("Tor client used before start().")
        began = time.monotonic()
        response = self._client.get(url)
        return FetchResult(
            url=str(response.url),
            status=response.status_code,
            headers=dict(response.headers),
            body=response.content[:MAX_RESPONSE_BYTES],
            elapsed_ms=int((time.monotonic() - began) * 1000),
        )

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            self.owns_process = False

    def __enter__(self) -> "TorClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

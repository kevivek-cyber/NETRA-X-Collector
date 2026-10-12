"""
The collector: the half of NETRA-X that is allowed to touch Tor.

Run this on a machine that can reach the Tor network. It asks the control
plane for queued crawls, performs them locally, and posts each page back as it
is fetched. Nothing listens; every connection is outbound, so this works from
behind NAT, on a hotspot, or on a laptop that moves between networks.

    set NETRAX_SERVER=https://console.onnetra.in
    set NETRAX_COLLECTOR_TOKEN=...
    python -m workers.collector_agent

Stop it with Ctrl+C. A job in flight is finished first where possible; if the
process dies outright the server's lease expires and the job returns to the
queue for whoever is next.

Why the collector pulls rather than the server pushing: a push requires the
server to reach this machine, which means an inbound port, a tunnel, or a
fixed address. None of those survive a laptop changing networks, and all of
them are things an operator would have to maintain. Polling costs one small
request every few seconds and needs nothing.
"""
from __future__ import annotations

import argparse
import base64
import logging
import os
import platform
import signal
import threading
import sys
import time
from typing import Optional

import httpx

from workers.collection.crawler import CrawlLimits, crawl
from workers.collection.tor_client import TorClient, TorUnavailable

LOG = logging.getLogger("collector")

# Image collection is capped in two directions: per page, so one gallery
# cannot consume a crawl, and per crawl, so a large site cannot fill the
# server's asset store. Each image is its own Tor request and most of the
# time cost of a crawl with images on is these fetches.
IMAGES_PER_PAGE = 8
IMAGES_PER_CRAWL = 48

POLL_IDLE_SECONDS = 5.0       # nothing queued: ask again shortly
POLL_ERROR_SECONDS = 20.0     # server unreachable: back off, do not hammer
HEARTBEAT_SECONDS = 5.0      # from a background thread, so it continues mid-crawl
AGENT_VERSION = "1.0"

_STOP = False


def _stop_requested() -> bool:
    """Whether something outside has asked this collector to stop.

    Signals are the obvious mechanism and do not work here. The operator
    console starts this process with CREATE_NO_WINDOW so no terminal flashes
    up, and a Windows process without a console cannot receive console control
    events -- CTRL_BREAK goes nowhere and the parent is left with kill(), which
    strands whatever job was in flight until its lease expires.

    A file the parent touches is cruder and actually arrives.
    """
    path = os.getenv("NETRAX_STOP_FILE")
    return bool(path) and os.path.exists(path)


def _handle_signal(signum, frame):  # noqa: ARG001
    global _STOP
    if _STOP:
        LOG.warning("second interrupt -- exiting now")
        sys.exit(1)
    _STOP = True
    LOG.info("stopping after the current job; interrupt again to quit immediately")


class ControlPlane:
    """Everything this agent says to the server."""

    def __init__(self, base_url: str, token: str, collector_id: str, timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.collector_id = collector_id
        self._client = httpx.Client(
            timeout=timeout,
            headers={"X-Collector-Token": token,
                     "X-Collector-Id": collector_id,
                     "User-Agent": f"netra-x-collector/{AGENT_VERSION}"},
        )

    def _post(self, path: str, payload: dict) -> dict:
        response = self._client.post(f"{self.base}/api/v1/collection/worker/{path}",
                                     json=payload)
        response.raise_for_status()
        return response.json() if response.content else {}

    def heartbeat(self, tor_ready: bool) -> dict:
        return self._post("heartbeat", {
            "collector_id": self.collector_id,
            "label": platform.node(),
            "tor_ready": tor_ready,
            "version": AGENT_VERSION,
        })

    def offline(self) -> None:
        """Tell the server this collector is going away. Best effort and
        quick: it runs while the process is being shut down."""
        try:
            self._client.post(f"{self.base}/api/v1/collection/worker/offline",
                              json={"collector_id": self.collector_id}, timeout=4.0)
        except Exception:                             # noqa: BLE001 - shutting down
            pass

    def claim(self) -> Optional[dict]:
        job = self._post("claim", {"collector_id": self.collector_id})
        return job or None

    def submit_page(self, job_id: str, page, images: Optional[list] = None) -> dict:
        return self._post("page", {
            "collector_id": self.collector_id,
            "job_id": job_id,
            "url": page.url,
            "depth": page.depth,
            "status": page.status,
            "title": page.title or "",
            "body": page.body.decode("utf-8", errors="replace"),
            "images": images or [],
        })

    def finish(self, job_id: str, state: str, message: str = "",
               errors: Optional[list] = None) -> dict:
        return self._post("finish", {
            "collector_id": self.collector_id,
            "job_id": job_id,
            "state": state,
            "message": message,
            "errors": errors or [],
        })

    def close(self) -> None:
        self._client.close()


def collect_images(tor, page, budget: int) -> list:
    """Fetch this page's images over Tor and hand back the bytes.

    The collector fetches; the server decides what to keep. A failure here is
    ordinary -- an image that times out, 404s or turns out not to be an image
    is skipped, never fatal to the crawl.

    Bytes travel base64 in the page payload rather than as a second request,
    because an image that arrives separately from the page it came from has to
    be reconciled, and there is no reason to make that anyone's problem.
    """
    collected = []
    for src in page.images[:IMAGES_PER_PAGE]:
        if len(collected) >= min(IMAGES_PER_PAGE, budget):
            break
        try:
            result = tor.fetch(src)
        except Exception:                             # noqa: BLE001
            continue
        if result.status != 200 or not result.body:
            continue
        collected.append({
            "src": src,
            "content_type": result.headers.get("content-type", ""),
            "body_b64": base64.b64encode(result.body).decode("ascii"),
        })
    return collected


def run_job(server: ControlPlane, job: dict) -> None:
    """Crawl one job end to end, reporting as we go."""
    job_id = job["job_id"]
    url = job["url"]
    limits = CrawlLimits(
        max_depth=int(job.get("max_depth", 2)),
        max_pages=int(job.get("max_pages", 50)),
    )
    LOG.info("claimed %s -> %s (depth %d, max %d pages)",
             job_id[:8], url, limits.max_depth, limits.max_pages)

    errors: list = []
    pages = 0
    fetch_images = bool(job.get("fetch_images"))
    images_left = IMAGES_PER_CRAWL if fetch_images else 0
    tor = TorClient()

    # Any failure to get a circuit fails the job, not the agent. Catching only
    # TorUnavailable was too narrow: a missing socksio raises ImportError, which
    # escaped and killed the process -- leaving the job CLAIMED until its lease
    # expired, with nothing in the console explaining why.
    try:
        tor.start()
    except Exception as exc:                          # noqa: BLE001
        LOG.error("could not start Tor: %s: %s", type(exc).__name__, exc)
        if isinstance(exc, ImportError) and "socks" in str(exc).lower():
            LOG.error("  this interpreter is missing SOCKS support -- "
                      "pip install 'httpx[socks]', or run the project venv")
        try:
            server.finish(job_id, "FAILED",
                          message=f"The collector has no working Tor circuit: {exc}"[:400],
                          errors=[f"{type(exc).__name__}: {exc}"])
        except httpx.HTTPError:
            LOG.error("could not report the failure; the lease will expire")
        return

    try:
        for page in crawl(url, tor.fetch, limits):
            if _STOP or _stop_requested():
                LOG.info("interrupted mid-crawl; reporting what was collected")
                break
            images = []
            if images_left > 0:
                images = collect_images(tor, page, images_left)
                images_left -= len(images)

            try:
                server.submit_page(job_id, page, images)
                pages += 1
                LOG.info("  [%d] %s (%d)%s", pages, page.url, page.status,
                         f" +{len(images)} image(s)" if images else "")
            except httpx.HTTPError as exc:
                # A page we fetched but could not hand over. Worth recording:
                # the crawl succeeded and the handoff did not, and those need
                # different fixes.
                LOG.warning("  could not submit %s: %s", page.url, exc)
                errors.append(f"submit failed for {page.url}: {exc}")

        server.finish(job_id, "DONE", errors=errors)
        LOG.info("finished %s -- %d page(s)", job_id[:8], pages)

    except Exception as exc:                          # noqa: BLE001
        LOG.exception("crawl failed")
        try:
            server.finish(job_id, "FAILED", message=str(exc)[:400],
                          errors=errors + [str(exc)])
        except httpx.HTTPError:
            LOG.error("could not report the failure; the lease will expire")
    finally:
        tor.stop()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="NETRA-X collector")
    parser.add_argument("--server", default=os.getenv("NETRAX_SERVER", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.getenv("NETRAX_COLLECTOR_TOKEN", ""))
    parser.add_argument("--id", dest="collector_id",
                        default=os.getenv("NETRAX_COLLECTOR_ID", "") or platform.node().lower())
    parser.add_argument("--once", action="store_true",
                        help="take at most one job, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO. This agent polls every few seconds, so
    # that buries the two lines an operator actually wants to see -- which job
    # was claimed, and which pages came back -- under a wall of 200 OKs.
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    if not args.token:
        parser.error("no collector token. Set NETRAX_COLLECTOR_TOKEN or pass --token.")

    signal.signal(signal.SIGINT, _handle_signal)
    # SIGBREAK is how a Windows parent asks a child in its own process group to
    # stop. Without it the operator console could only kill this process, and a
    # kill mid-crawl strands the job it holds.
    for name in ("SIGTERM", "SIGBREAK"):
        handler = getattr(signal, name, None)
        if handler is not None:
            try:
                signal.signal(handler, _handle_signal)
            except (OSError, ValueError):
                pass                                  # not available everywhere

    server = ControlPlane(args.server, args.token, args.collector_id)
    LOG.info("collector %s -> %s", args.collector_id, args.server)

    # Say hello before claiming anything, so the server stops alerting the
    # operator the moment this process is up rather than when work arrives.
    tor_ready = _probe_tor()
    last_beat = float("inf")       # the background thread owns heartbeats now

    # Heartbeats run on their own thread with their own connection, every few
    # seconds, so a collector deep in a slow Tor crawl still shows as online
    # and the server can use a short online window for one that died.
    beats_stop = threading.Event()
    beat_plane = ControlPlane(args.server, args.token, args.collector_id, timeout=10.0)

    def _beat_loop() -> None:
        while not beats_stop.is_set():
            try:
                beat_plane.heartbeat(tor_ready)
            except httpx.HTTPError as exc:
                LOG.warning("heartbeat failed: %s", exc)
            beats_stop.wait(HEARTBEAT_SECONDS)

    threading.Thread(target=_beat_loop, name="heartbeat", daemon=True).start()

    # Closing the console window (the X) is the usual way an operator stops
    # this on Windows, and no Python signal fires for it. A console control
    # handler does: it gets a few seconds to tell the server first.
    _install_close_handler(server)

    try:
        while not _STOP:
            if _stop_requested():
                LOG.info("stop requested by the operator console")
                break
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_SECONDS:
                try:
                    server.heartbeat(tor_ready)
                    last_beat = now
                except httpx.HTTPError as exc:
                    LOG.warning("heartbeat failed: %s", exc)
                    time.sleep(POLL_ERROR_SECONDS)
                    continue

            try:
                job = server.claim()
            except httpx.HTTPError as exc:
                LOG.warning("could not reach the server: %s", exc)
                time.sleep(POLL_ERROR_SECONDS)
                continue

            if not job:
                time.sleep(POLL_IDLE_SECONDS)
                continue

            run_job(server, job)
            last_beat = 0.0                           # beat again straight away
            if args.once:
                break
    finally:
        beats_stop.set()
        server.offline()
        server.close()
        beat_plane.close()

    LOG.info("collector stopped")
    return 0


_CLOSE_HANDLER = None   # kept referenced so the callback is not collected


def _install_close_handler(server: "ControlPlane") -> None:
    """On Windows, report offline when the console window is closed."""
    global _CLOSE_HANDLER
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _on_event(event: int) -> bool:
            # 2 = window closed, 5 = log off, 6 = shut down.
            if event in (2, 5, 6):
                server.offline()
            return False                              # let Windows proceed

        _CLOSE_HANDLER = HANDLER(_on_event)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CLOSE_HANDLER, True)
    except Exception:                                 # noqa: BLE001 - optional nicety
        pass


def _probe_tor() -> bool:
    """Whether Tor looks usable, without holding a circuit open."""
    from workers.collection.tor_client import find_running_proxy, find_tor_binary
    return bool(find_running_proxy() or find_tor_binary())


if __name__ == "__main__":
    raise SystemExit(main())

"""
The crawl itself: which pages get fetched, in what order, and when to stop.

Deliberately free of network code. `crawl` takes the fetch function, so the
rules that decide where a crawl goes are tested without Tor, and the same
logic can be pointed at a local fixture site.

The hard rule is same-host. A link to another onion service is recorded as a
discovered edge -- useful intelligence, and the graph wants it -- but never
queued, so one typed address cannot turn into an unbounded tour of the
darknet.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterator, List, Optional
from urllib.parse import urldefrag, urljoin, urlparse

from workers.collection.tor_client import FetchResult


@dataclass
class CrawlLimits:
    max_depth: int = 2
    max_pages: int = 50
    delay: float = 1.0


@dataclass
class CrawledPage:
    url: str
    depth: int
    status: int
    body: bytes
    content_hash: str
    links: List[str] = field(default_factory=list)
    off_host_links: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    title: str = ""
    text: str = ""


class _PageParser(HTMLParser):
    """One pass over a page: its links, image sources, title and visible text.

    Everything the preview and the extractor need comes from here, so a page is
    parsed once rather than three times. Script and style contents are dropped
    from the text -- they are code, not what a person reading the page sees.
    """

    _SKIP_TEXT = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: List[str] = []
        self.img_srcs: List[str] = []
        self.title = ""
        self._text_parts: List[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "a" and attr.get("href"):
            self.hrefs.append(attr["href"])
        elif tag == "img":
            # data-src / data-original catch the lazy-loaded ones too.
            src = attr.get("src") or attr.get("data-src") or attr.get("data-original")
            if src:
                self.img_srcs.append(src)
        elif tag == "title":
            self._in_title = True
        if tag in self._SKIP_TEXT:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP_TEXT and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0:
            chunk = data.strip()
            if chunk:
                self._text_parts.append(chunk)

    @property
    def text(self) -> str:
        return " ".join(self._text_parts)


def onion_host(url: str) -> Optional[str]:
    """The onion hostname of a URL, or None when it is not an onion address."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    return host if host.endswith(".onion") else None


def normalise(url: str) -> str:
    """Drop the fragment: /a#top and /a are one page, fetched once."""
    bare = urldefrag(url)[0]
    trimmed = bare.rstrip("/")
    return trimmed or bare


def _parse(html: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(html)
    except Exception:
        # Malformed markup is the norm on the sites this crawls; whatever was
        # parsed before the break is still worth having.
        pass
    return parser


def _absolute_links(base_url: str, hrefs: List[str], keep_fragment: bool = False) -> List[str]:
    out: List[str] = []
    for href in hrefs:
        if href.lower().startswith(("mailto:", "javascript:", "tel:", "data:", "#")):
            continue
        try:
            resolved = urljoin(base_url, href)
        except ValueError:
            continue
        absolute = resolved if keep_fragment else normalise(resolved)
        if absolute not in out:
            out.append(absolute)
    return out


def extract_links(base_url: str, html: str) -> List[str]:
    return _absolute_links(base_url, _parse(html).hrefs)


def extract_images(base_url: str, html: str) -> List[str]:
    """Absolute image URLs referenced by the page (src / data-src)."""
    return _absolute_links(base_url, _parse(html).img_srcs, keep_fragment=True)


def extract_title(html: str) -> str:
    return re.sub(r"\s+", " ", _parse(html).title).strip()


def extract_text(html: str, limit: int = 4000) -> str:
    """The page's visible text, collapsed and capped."""
    text = re.sub(r"\s+", " ", _parse(html).text).strip()
    return text[:limit]


def crawl(
    start_url: str,
    fetch: Callable[[str], FetchResult],
    limits: CrawlLimits = CrawlLimits(),
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[CrawledPage]:
    """Yield pages breadth-first, staying on the starting host."""
    host = onion_host(start_url)
    start = normalise(start_url)
    queue = deque([(start, 0)])
    seen_urls = {start}
    seen_bodies = set()
    fetched = 0

    while queue and fetched < limits.max_pages:
        url, depth = queue.popleft()
        if fetched and limits.delay:
            sleep(limits.delay)
        try:
            result = fetch(url)
        except Exception:
            # A page that times out costs that page, not the crawl.
            continue
        fetched += 1

        body = result.body or b""
        content_hash = hashlib.sha256(body).hexdigest()
        html = body.decode("utf-8", errors="replace")

        parsed = _parse(html)
        links = _absolute_links(url, parsed.hrefs)
        on_host = [link for link in links if onion_host(link) == host]
        off_host = [link for link in links if onion_host(link) != host]
        # Same-host images only: an <img> pointing off the site is a CDN or a
        # tracker, and the containment rule that governs links governs these too.
        images = [img for img in _absolute_links(url, parsed.img_srcs, keep_fragment=True)
                  if onion_host(img) == host]

        if content_hash not in seen_bodies:
            seen_bodies.add(content_hash)
            yield CrawledPage(
                url=url,
                depth=depth,
                status=result.status,
                body=body,
                content_hash=content_hash,
                links=on_host,
                off_host_links=off_host,
                images=images,
                title=re.sub(r"\s+", " ", parsed.title).strip(),
                text=re.sub(r"\s+", " ", parsed.text).strip()[:4000],
            )

        if depth < limits.max_depth:
            for link in on_host:
                if link not in seen_urls:
                    seen_urls.add(link)
                    queue.append((link, depth + 1))

"""Invoke automation tasks for omerta development.

List them with ``uv run inv --list``.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import io
import json
import posixpath
import re
import shutil
import socket
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html import escape
from http import HTTPStatus
from pathlib import Path

from invoke import task
from invoke.context import Context
from invoke.exceptions import Exit

_ROOT = Path(__file__).parent
_README = _ROOT / "README.md"

# The repository's social preview: the drawing, and the picture made from it.
_SOCIAL_PREVIEW = _ROOT / "social" / "github-social-preview.svg"

# The browsers that can take that picture, by the names they have on a PATH
# and by where Windows and macOS install them.
_BROWSER_NAMES = (
    "chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "msedge",
    "microsoft-edge",
)
_BROWSER_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


def _port_in_use(port: int) -> bool:
    """Whether something already answers on ``localhost:port``."""
    for family, kind, proto, _, address in socket.getaddrinfo(
        "localhost", port, type=socket.SOCK_STREAM
    ):
        with socket.socket(family, kind, proto) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(address) == 0:
                return True
    return False


def _find_browser() -> str | None:
    """A Chrome, Edge or Chromium on this machine, or ``None``."""
    for name in _BROWSER_NAMES:
        if found := shutil.which(name):
            return found
    return next((path for path in _BROWSER_PATHS if Path(path).is_file()), None)


# GitHub's Markdown API renders the README's HTML, in the mode of a file in a
# repository. Without a token it answers 60 requests an hour.
_MARKDOWN_API = "https://api.github.com/markdown"

# That mode renders an alert (`> [!NOTE]`) as a quote with its marker as text,
# where the repository's page shows an alert. The alerts are taken, in order,
# from a second rendering in the mode of a comment ("gfm"), which is sent only
# for a README that has one.
_ALERT_MARKER = re.compile(r"^ {0,3}> ?\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", re.I | re.M)
_ALERT_QUOTE = re.compile(r"<blockquote>\s*<p>\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", re.I)
_ALERT = re.compile(r'<div class="markdown-alert ')

# github-markdown-css gives the page GitHub's look, one stylesheet per theme;
# npm installs it (package.json).
_MARKDOWN_CSS = "node_modules/github-markdown-css/github-markdown-{scheme}.css"

# The preview's two pages, each in one of GitHub's themes whatever the browser
# is set to, so that both can be open side by side, with GitHub's ground in it.
# The light one is served on the task's port, the dark one on the next.
_PREVIEW_SCHEMES = {"light": "#ffffff", "dark": "#0d1117"}

# What the preview serves besides its page: the stylesheets, and the folder the
# README's images would come from. Nothing else in the repository is reachable.
_PREVIEW_PATHS = ("node_modules/github-markdown-css/", "assets/")

# The page around GitHub's HTML. The column is as wide as the README's on the
# repository's page on GitHub at 1280 pixels and wider, and below 768, where the
# sidebar goes under it; in between, GitHub's is narrower. Links are underlined,
# as GitHub has them.
_PREVIEW_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="{scheme}">
<title>README.md · omerta · {scheme}</title>
<link rel="stylesheet" href="/{stylesheet}">
<style>
  body {{ margin: 0; background: {background}; }}
  .markdown-body {{
    box-sizing: content-box; max-width: 838px; margin: 0 auto; padding: 32px 33px;
  }}
  .markdown-body a {{ text-decoration: underline; }}
</style>
</head>
<body>
<article class="markdown-body">
<!-- README -->
</article>
</body>
</html>
"""


class _RenderError(Exception):
    """GitHub did not render the README; the message says why."""


@functools.lru_cache(maxsize=1)
def _render_readme(text: str) -> str:
    """The HTML GitHub makes of ``text`` as a repository's README.

    The last result is kept, so a page reload asks GitHub again only when the
    README has changed.
    """
    html = _render(text, "markdown")
    if _ALERT_MARKER.search(text):
        html = _with_alerts(html, _render(text, "gfm"))
    return html


def _with_alerts(html: str, comment: str) -> str:
    """``html`` with each alert's quote replaced by the alert of ``comment``, in order."""
    quotes = _elements(html, _ALERT_QUOTE, "blockquote")
    alerts = [comment[start:end] for start, end in _elements(comment, _ALERT, "div")]
    if len(quotes) != len(alerts):
        raise _RenderError(
            f"GitHub rendered {len(quotes)} alerts as quotes, and {len(alerts)} as alerts."
        )
    for (start, end), alert in reversed(list(zip(quotes, alerts, strict=True))):
        html = html[:start] + alert + html[end:]
    return html


def _elements(html: str, opening: re.Pattern[str], tag: str) -> list[tuple[int, int]]:
    """Where each element that ``opening`` starts begins and ends, counting nested ``tag``s."""
    tags = re.compile(rf"<(/?){tag}\b[^>]*>")
    spans = []
    for found in opening.finditer(html):
        depth = 0
        for each in tags.finditer(html, found.start()):
            depth += -1 if each[1] else 1
            if depth == 0:
                spans.append((found.start(), each.end()))
                break
    return spans


def _render(text: str, mode: str) -> str:
    """The HTML GitHub's Markdown API makes of ``text`` in ``mode``."""
    request = urllib.request.Request(
        _MARKDOWN_API,
        data=json.dumps({"text": text, "mode": mode}).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        # A fixed https address of GitHub's API
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.headers.get("x-ratelimit-remaining") == "0":
            reset = datetime.fromtimestamp(int(error.headers.get("x-ratelimit-reset", "0")))
            raise _RenderError(
                f"GitHub's limit of 60 requests an hour is used up until {reset:%H:%M}."
            ) from error
        body = error.read().decode("utf-8", "replace")
        raise _RenderError(f"GitHub's Markdown API answered {error.code}: {body}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise _RenderError(f"GitHub's Markdown API could not be reached: {error}") from error


class _ReadmePreview(http.server.SimpleHTTPRequestHandler):
    """Serves README.md as GitHub renders it at ``/``, and the files its page loads."""

    def __init__(self, *args, scheme: str, **kwargs) -> None:
        # Set before the base class's __init__, which handles the request.
        self.scheme = scheme
        super().__init__(*args, **kwargs)

    # Windows can map these types to others in its registry, and a browser
    # refuses a stylesheet that is not served as text/css.
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".css": "text/css",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    }

    def send_head(self):
        """Answer ``/`` with the page, a path under ``_PREVIEW_PATHS`` with its file."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        path = posixpath.normpath(path).lstrip("/")
        if not path:
            return self._send_page()
        if path.startswith(_PREVIEW_PATHS):
            return super().send_head()
        self.send_error(HTTPStatus.NOT_FOUND, "The preview serves the README and its images only")
        return None

    def end_headers(self) -> None:
        """Ask the browser not to keep anything, so a reload shows an edited image."""
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_page(self) -> io.BytesIO:
        """Send the headers of the page, and return its body."""
        try:
            html = _render_readme(_README.read_text(encoding="utf-8"))
            frame = _PREVIEW_PAGE.format(
                scheme=self.scheme,
                stylesheet=_MARKDOWN_CSS.format(scheme=self.scheme),
                background=_PREVIEW_SCHEMES[self.scheme],
            )
            status, page = HTTPStatus.OK, frame.replace("<!-- README -->", html)
        except _RenderError as error:
            print(error)
            status, page = HTTPStatus.BAD_GATEWAY, f"<pre>{escape(str(error))}</pre>"
        body = page.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)


@task(name="readme-preview")
def readme_preview(c: Context, port: int = 9120) -> None:
    """
    Serve README.md as GitHub renders it, to look at before it is pushed.

    GitHub's Markdown API makes the HTML, so what GitHub removes is removed,
    and github-markdown-css gives it GitHub's look (``npm ci`` installs it):
    light on the port, dark on the next one. A reload renders the README again
    when it has changed, one request to GitHub for both pages, two where the
    README has an alert: 60 an hour are allowed without a token.

    Args:
        port: Port of the light page; the dark one is on the next (default: 9120)

    Example:
        uv run inv readme-preview
        uv run inv readme-preview --port 9122
    """
    if not all((_ROOT / _MARKDOWN_CSS.format(scheme=s)).is_file() for s in _PREVIEW_SCHEMES):
        raise Exit("The preview needs github-markdown-css. Install it first: npm ci", code=1)
    ports = {scheme: port + offset for offset, scheme in enumerate(_PREVIEW_SCHEMES)}
    if busy := [p for p in ports.values() if _port_in_use(p)]:
        raise Exit(
            f"Port {busy[0]} is in use by another program. Pass a free pair: "
            f"uv run inv readme-preview --port {busy[-1] + 1}",
            code=1,
        )
    with contextlib.ExitStack() as stack:
        servers = []
        for scheme, scheme_port in ports.items():
            handler = functools.partial(_ReadmePreview, scheme=scheme, directory=str(_ROOT))
            # 127.0.0.1, not localhost: on Windows a request to localhost tries
            # IPv6 first and waits two seconds for the refusal.
            address = ("127.0.0.1", scheme_port)
            servers.append(stack.enter_context(http.server.ThreadingHTTPServer(address, handler)))
            print(f"Serving README.md {scheme} at http://127.0.0.1:{scheme_port}/", flush=True)
        # The first page is served in this thread, where Ctrl+C reaches it; the
        # others are stopped before their sockets are closed.
        for server in servers[1:]:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.shutdown)
        print("Ctrl+C stops them", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            servers[0].serve_forever()


@task(name="social-preview")
def social_preview(c: Context, browser: str = "") -> None:
    """
    Render social/github-social-preview.png from the SVG beside it.

    A headless Chrome, Edge or Chromium takes the picture, at 1280 by 640
    pixels. GitHub reads no file for it: upload the PNG in the repository's
    settings.

    Args:
        browser: Path of the browser, where none is found

    Example:
        uv run inv social-preview
        uv run inv social-preview --browser /usr/bin/brave-browser
    """
    browser = browser or _find_browser() or ""
    if not browser:
        raise Exit(
            "No Chrome, Edge or Chromium was found. Name one: "
            "uv run inv social-preview --browser PATH",
            code=1,
        )
    png = _SOCIAL_PREVIEW.with_suffix(".png")
    with tempfile.TemporaryDirectory() as scratch:
        shot = Path(scratch) / png.name
        # The browser takes absolute paths only, and exits with 0 when it
        # wrote nothing. --disable-lcd-text keeps the coloured fringes of
        # sub-pixel text rendering out of the picture.
        result = c.run(
            f'"{browser}" --headless --disable-lcd-text --hide-scrollbars'
            " --force-device-scale-factor=1 --window-size=1280,640"
            f' "--screenshot={shot}" "{_SOCIAL_PREVIEW.as_uri()}"',
            hide=True,
            warn=True,
        )
        if not shot.is_file():
            raise Exit(f"{browser} wrote no image.\n{result.stderr}", code=1)
        shutil.copyfile(shot, png)
    print(f"Wrote {png.relative_to(_ROOT).as_posix()}")

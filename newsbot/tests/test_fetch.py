import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsbot as nb

RSS = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>'
       b'<item><title>Hello</title><link>http://x/1</link></item></channel></rss>')


class Handler(BaseHTTPRequestHandler):
    """/stall stalls non-browsers (like CBC), /forbid 403s them, /missing is a plain 404."""

    def do_GET(self):
        browser = self.headers.get("User-Agent", "").startswith("Mozilla/")
        if self.path == "/missing" or (self.path == "/forbid" and not browser):
            self.send_response(404 if self.path == "/missing" else 403)
            self.end_headers()
            return
        if self.path == "/stall" and not browser:
            time.sleep(1.5)
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(RSS)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def fetch(monkeypatch):
    real_get = requests.Session.get
    monkeypatch.setattr(requests.Session, "get",
                        lambda self, url, timeout, **kw: real_get(self, url, timeout=(1, 0.5), **kw))
    return nb.make_fetcher()


@pytest.mark.parametrize("path", ["/stall", "/forbid"])
def test_falls_back_to_browser_user_agent(server, fetch, path):
    feed = fetch(server + path)
    assert feed.entries[0].title == "Hello"


def test_not_found_is_not_retried_as_browser(server, fetch):
    with pytest.raises(requests.HTTPError):
        fetch(server + "/missing")

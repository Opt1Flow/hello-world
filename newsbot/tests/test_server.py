import base64
import hashlib
import http.client
import json
import re
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsbot as nb
import server
from test_newsbot import at, cfg, db, fake_fetch  # noqa: F401  (pytest fixtures)


@pytest.fixture
def running(cfg):
    srv = server.make_server(cfg, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()
    srv.server_close()


def request(port, path, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), body


def test_pages_and_data_are_served(cfg, db, running):
    feeds = {"https://a.test/rss": [("Council approves bridge", "https://a.test/1", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    status, headers, body = request(running, "/")
    assert status == 200 and b"Council approves bridge" in body
    status, headers, body = request(running, "/news.json")
    data = json.loads(body)
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "*"
    assert headers["Cache-Control"] == "no-store"
    assert data["timezone"] == "America/Edmonton" and data["next"]["name"] == "Afternoon"
    morning = next(e for e in data["editions"] if e["name"] == "Morning")
    assert morning["kind"] == "ok" and morning["subjects"]["Local"][0]["title"] == "Council approves bridge"


def test_kiosk_script_is_allowed_by_its_policy_and_nothing_else(running):
    status, headers, body = request(running, "/kiosk")
    assert status == 200
    script = re.search(rb"<script>(.*?)</script>", body, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
    policy = headers["Content-Security-Policy"]
    assert f"script-src 'sha256-{digest}'" in policy and "default-src 'none'" in policy


@pytest.mark.parametrize("path", ["/config.toml", "/data/state.db", "/data/newsbot.log", "/news.md",
                                  "/../config.toml", "/%2e%2e/config.toml", "/newsbot.py", "/kiosk/../config.toml"])
def test_nothing_else_in_the_folder_can_be_fetched(running, path):
    status, _, body = request(running, path)
    assert status == 404 and b"[" not in body


def test_read_only(running):
    assert request(running, "/news.json", method="POST")[0] == 501
    assert request(running, "/news.json", method="DELETE")[0] == 501


def test_before_the_first_edition(running):
    status, _, body = request(running, "/news.json")
    assert status == 503 and b"No edition yet" in body


def test_head_has_no_body(cfg, db, running):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    status, headers, body = request(running, "/news.json", method="HEAD")
    assert status == 200 and body == b"" and int(headers["Content-Length"]) > 0

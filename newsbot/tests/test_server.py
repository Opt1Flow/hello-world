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


# ---- review fixes

def test_windows_line_endings_still_match_the_policy(cfg, tmp_path, monkeypatch):
    crlf = tmp_path / "kiosk.html"
    crlf.write_bytes(server.KIOSK.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    monkeypatch.setattr(server, "KIOSK", crlf)
    srv = server.make_server(cfg, 0, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _, headers, body = request(srv.server_port, "/kiosk")
    finally:
        srv.shutdown()
        srv.server_close()
    script = re.search(rb"<script>(.*?)</script>", body, re.S).group(1).replace(b"\r\n", b"\n")
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode()   # what Chromium computes
    assert f"'sha256-{digest}'" in headers["Content-Security-Policy"]


def test_kiosk_is_told_the_timezone(running):
    _, _, body = request(running, "/kiosk")
    assert b'<html lang="en" data-tz="America/Edmonton">' in body


def test_server_follows_config_changes(cfg, db, tmp_path):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    moved = dict(cfg, html_output=tmp_path / "vault" / "News.html", json_output=tmp_path / "vault" / "News.json")
    moved["json_output"].parent.mkdir()
    moved["json_output"].write_text('{"moved": true}')
    config = tmp_path / "config.toml"
    config.write_text("x")
    current = {"cfg": cfg}
    srv = server.make_server(cfg, 0, host="127.0.0.1", loader=lambda: current["cfg"], config_path=config)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert b'"moved"' not in request(srv.server_port, "/news.json")[2]
        current["cfg"] = moved
        config.write_text("changed")
        import os
        os.utime(config, (1, 1))   # a different modification time
        assert b'"moved": true' in request(srv.server_port, "/news.json")[2]
    finally:
        srv.shutdown()
        srv.server_close()


def test_bad_config_keeps_the_last_good_settings(cfg, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("x")
    def broken():
        raise ValueError("typo in config.toml")
    live = server.LiveConfig(cfg, config, broken)
    import os
    os.utime(config, (5, 5))
    assert live.get() is cfg


def test_upgrade_creates_news_json_at_the_next_check_in(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    cfg["json_output"].unlink()                                    # as before the kiosk existed
    assert not nb.run_due(cfg, db, at("2026-10-08 06:20"), fake_fetch({}))
    assert cfg["json_output"].exists()


def test_news_json_written_even_if_the_page_cannot_be(cfg, db):
    cfg["html_output"].mkdir()                                     # can't be replaced by a file
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    assert json.loads(cfg["json_output"].read_text())["editions"]


def test_shared_problem_messages_have_no_paths(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    cfg["output"].write_text(cfg["output"].read_text().replace("<!-- Afternoon end -->", ""))
    with pytest.raises(nb.MarkerError):
        nb.run_due(cfg, db, at("2026-10-08 13:05"), fake_fetch({}))
    problem = json.loads(cfg["json_output"].read_text())["problem"]["message"]
    assert str(cfg["output"].parent) not in problem and "newsbot.log on the laptop" in problem

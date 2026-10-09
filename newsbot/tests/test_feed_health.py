import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsbot as nb
from test_newsbot import at, cfg, db, fake_fetch  # noqa: F401  (pytest fixtures)

RSS = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
       b'<item><title>Hello</title><link>http://x.test/1</link></item></channel></rss>')


def test_unchanged_feed_is_not_downloaded_again():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("If-None-Match"))
            if self.headers.get("If-None-Match") == '"v1"':
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.end_headers()
            self.wfile.write(RSS)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/feed"
    try:
        first = nb.make_fetcher()
        assert first(url).entries[0].title == "Hello"
        second = nb.make_fetcher(first.validators)       # the next run, primed
        assert second(url).not_modified
    finally:
        srv.shutdown()
    assert seen == [None, '"v1"']


def test_validators_survive_between_runs_and_skip_alert_feeds(cfg, db):
    cfg["alerts"] = {"Weather": ["https://w.test/rss"]}
    fetch = fake_fetch({})
    fetch.validators = {"https://a.test/rss": ('"e1"', None), "https://w.test/rss": ('"w"', None)}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fetch)
    primed = nb.news_fetcher(cfg, db).validators
    assert primed == {"https://a.test/rss": ('"e1"', None)}


def test_not_modified_counts_as_a_healthy_answer(cfg, db):
    def fetch(url):
        return nb.NOT_MODIFIED
    assert nb.run_due(cfg, db, at("2026-10-08 06:05"), fetch)
    assert nb.feed_health(cfg, db, at("2026-10-08 06:05")) == []


def test_failing_feed_is_flagged_after_three_runs(cfg, db):
    cfg["sources"] = {"https://c.test/rss": "World Paper"}
    bad = fake_fetch({}, fail={"https://c.test/rss"})
    for t in ("2026-10-08 06:05", "2026-10-08 13:05"):
        nb.run_due(cfg, db, at(t), bad)
    assert nb.feed_health(cfg, db, at("2026-10-08 13:05")) == []      # two misses: not yet
    nb.run_due(cfg, db, at("2026-10-08 19:05"), bad)
    [h] = nb.feed_health(cfg, db, at("2026-10-08 19:05"))
    assert h["name"] == "World Paper" and "no answer the last 3 times" in h["problem"]
    assert "Source check: World Paper (World)" in cfg["html_output"].read_text()
    nb.run_due(cfg, db, at("2026-10-09 06:05"), fake_fetch({}))       # back: cleared
    assert nb.feed_health(cfg, db, at("2026-10-09 06:05")) == []


def test_feed_that_stops_publishing_is_flagged(cfg, db):
    story = {"https://a.test/rss": [("Story", "https://a.test/1", None)]}
    nb.run_due(cfg, db, at("2026-10-06 06:05"), fake_fetch(story))   # a.test has news
    for t in ("2026-10-07 06:05", "2026-10-08 06:05"):
        nb.run_due(cfg, db, at(t), fake_fetch(story))                # same story, nothing new
    later = at("2026-10-08 06:05") + timedelta(minutes=10)
    problems = {h["name"]: h["problem"] for h in nb.feed_health(cfg, db, later)}
    assert problems.get("a.test") == "no new stories since Tue 06 Oct"

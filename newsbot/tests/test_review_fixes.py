"""Regression tests for problems found in the review of the reading page."""
import fcntl
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsbot as nb
from test_newsbot import at, cfg, db, fake_fetch  # noqa: F401  (pytest fixtures)


def page(cfg):
    return cfg["html_output"].read_text(encoding="utf-8")


def raw_fetch(entries_by_url, titles=None):
    """Like fake_fetch, but entries are given as raw feedparser-style dicts."""
    def fetch(url):
        return SimpleNamespace(feed={"title": (titles or {}).get(url, url.split("/")[2])},
                               entries=entries_by_url.get(url, []))
    return fetch


# ---- one bad entry must never block an edition

def test_malformed_link_is_skipped_not_fatal(cfg, db):
    fetch = raw_fetch({"https://a.test/rss": [{"title": "Bad host", "link": "https://[www.cbc.ca]/1"},
                                              {"title": "Good story", "link": "https://a.test/2"}]})
    assert nb.run_due(cfg, db, at("2026-10-08 06:05"), fetch)
    assert "Good story" in page(cfg) and "Bad host" not in page(cfg)


def test_impossible_date_is_ignored_not_fatal(cfg, db):
    fetch = raw_fetch({"https://a.test/rss": [
        {"title": "Zero date", "link": "https://a.test/1", "published_parsed": (0, 0, 0, 0, 0, 0, 0, 0, 0)},
        {"title": "Good story", "link": "https://a.test/2"}]})
    assert nb.run_due(cfg, db, at("2026-10-08 06:05"), fetch)
    assert "Zero date" in page(cfg) and "Good story" in page(cfg)


# ---- feed links can't damage news.md

def test_link_cannot_fake_edition_markers(cfg, db):
    nb.run_due(cfg, db, at("2026-10-07 13:05"), fake_fetch({}))
    path = cfg["output"]
    path.write_text(path.read_text().replace("<!-- Morning end -->", "<!-- Morning end -->\n\nMY NOTES\n"))
    evil = "https://news.test/3#<!-- Morning end --><!-- Evening start -->"
    feeds = {"https://a.test/rss": [("Story", evil, None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))   # also stubs the missed Evening
    text = path.read_text()
    assert "MY NOTES" in text
    for name in cfg["editions"]:
        assert text.count(f"<!-- {name} start -->") == 1 and text.count(f"<!-- {name} end -->") == 1


def test_markdown_gets_no_extra_links_or_autolinks(cfg, db):
    feeds = {"https://a.test/rss": [("Story", "https://x.test/)[a](file:///C:/x)", None),
                                    ("Read the <search-ms:query=x> report", "https://a.test/2", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    md = cfg["output"].read_text()
    assert "](file:" not in md                                       # no second, local-file link
    assert "- Story — [a.test](https://x.test/%29[a]%28file:///C:/x%29)" in md
    assert not re.search(r"(?<!\\)<search-ms:", md)                 # escaped, so not an autolink


def test_relative_links_are_resolved_not_dropped():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    rss = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
           b'<item><title>Rel</title><link>/news/1</link></item>'
           b'<item><title>Proto</title><link>//other.test/2</link></item></channel></rss>')

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(rss)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        feed = nb.make_fetcher()(f"http://127.0.0.1:{srv.server_port}/feed.xml")
    finally:
        srv.shutdown()
    links = [nb.clean_link(e.link) for e in feed.entries]
    assert links == [f"http://127.0.0.1:{srv.server_port}/news/1", "http://other.test/2"]


# ---- upgrading from the version without the reading page

def test_editions_made_before_the_page_existed(cfg, db):
    nb.record_run(db, nb.recent_slots(at("2026-10-08 06:05"), cfg)[-1][0], "Morning", "ok",
                  at("2026-10-08 06:05"))
    assert not nb.run_due(cfg, db, at("2026-10-08 10:15"), fake_fetch({}))   # already done
    html = page(cfg)                                                          # but page now exists
    assert "before this reading page existed" in html
    assert "No editions yet" not in html


# ---- banners

def test_manual_run_offline_leaves_no_stuck_banner(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    offline = fake_fetch({}, fail={"https://a.test/rss", "https://b.test/rss"})
    assert not nb.run_now(cfg, db, "Morning", at("2026-10-08 09:00"), offline)
    nb.write_html(cfg, db, at("2026-10-08 09:15"))
    assert 'class="banner"' not in page(cfg)


# ---- page details

def test_skipped_editions_never_get_the_new_dot(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 13:20"), fake_fetch({}))
    html = page(cfg)
    skipped = html[html.index('id="ed-evening"'):].split(">", 1)[0]
    assert "data-produced" not in skipped


def test_next_slot_sorts_times_numerically(cfg):
    cfg["editions"] = {"Evening": "19:00", "Morning": "6:00"}
    assert nb.next_slot(at("2026-10-08 05:00"), cfg)[1] == "Morning"


def test_status_carries_times_for_the_live_clock(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    html = page(cfg)
    assert 'data-updated="2026-10-08T12:05:00+00:00"' in html
    assert 'data-next="2026-10-08T13:00:00-06:00" data-next-name="Afternoon"' in html


def test_missing_sources_named_with_subjects(cfg, db):
    cfg["sources"] = {"https://a.test/rss": "Paper A"}
    cfg["subjects"]["World"].append("https://d.test/rss")
    fetch = fake_fetch({}, fail={"https://a.test/rss"})
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fetch)
    assert "Missing this time: Paper A (Local)" in page(cfg)
    assert "Missing this time: Paper A (Local)" in cfg["output"].read_text()


# ---- weather warnings

ALL_CLEAR = {"title": "No watches or warnings in effect, City of Calgary",
             "link": "https://weather.test/report"}
WARNING = {"title": "SNOWFALL WARNING IN EFFECT, City of Calgary", "link": "https://weather.test/report"}


def test_warning_reusing_the_all_clear_link_is_shown_in_every_edition(cfg, db):
    cfg["alerts"] = {"Weather": ["https://weather.test/rss"]}
    calm = raw_fetch({"https://weather.test/rss": [ALL_CLEAR]})
    nb.run_due(cfg, db, at("2026-10-07 19:05"), calm)
    assert "Weather: no warnings in effect" in page(cfg)
    assert "No watches or warnings" not in page(cfg)                      # not a "story"

    snow = raw_fetch({"https://weather.test/rss": [WARNING]})
    nb.run_due(cfg, db, at("2026-10-08 06:05"), snow)
    nb.run_due(cfg, db, at("2026-10-08 13:05"), snow)                    # still in effect
    html = page(cfg)
    afternoon = html[html.index('id="ed-afternoon"'):html.index('id="ed-evening"')]
    assert "Weather warning in effect" in afternoon and "SNOWFALL WARNING" in afternoon
    assert "SNOWFALL WARNING IN EFFECT" in cfg["output"].read_text()


def test_alert_feed_down_is_said_plainly(cfg, db):
    cfg["alerts"] = {"Weather": ["https://weather.test/rss"]}
    fetch = fake_fetch({}, fail={"https://weather.test/rss"})
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fetch)
    assert "Weather warnings could not be checked this time" in page(cfg)


# ---- `open` while a run holds the lock

def test_open_while_a_run_is_busy_opens_the_existing_page(cfg, db, monkeypatch, tmp_path):
    monkeypatch.setattr(nb, "DATA_DIR", tmp_path)
    opened = []
    monkeypatch.setattr(nb.webbrowser, "open", opened.append)
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    with open(tmp_path / "newsbot.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert nb.open_page(cfg, at("2026-10-08 06:30")) == 0
    assert opened == [cfg["html_output"].as_uri()]

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsbot as nb
import ui
from test_newsbot import at, cfg, db, fake_fetch  # noqa: F401  (pytest fixtures)


def page(cfg):
    return cfg["html_output"].read_text(encoding="utf-8")


def test_page_written_with_latest_edition_selected(cfg, db):
    feeds = {"https://a.test/rss": [("Council approves bridge", "https://a.test/1", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    html = page(cfg)
    assert "Council approves bridge" in html
    assert re.search(r'id="ed-morning"[^>]*data-latest', html)
    assert 'href="https://a.test/1"' in html and 'rel="noopener noreferrer"' in html
    assert "{{" not in html                                        # every placeholder filled


def test_hostile_headline_is_escaped(cfg, db):
    evil = '<img src=x onerror=alert(1)> "quoted" {{BODY}}'
    feeds = {"https://a.test/rss": [(evil, "https://a.test/1", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    html = page(cfg)
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt; &quot;quoted&quot; {{BODY}}" in html


def test_non_web_links_are_dropped(cfg, db):
    feeds = {"https://a.test/rss": [("Click me", "javascript:alert(1)", None),
                                    ("Local file", "file:///C:/secret.txt", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    assert "javascript:" not in page(cfg) and "Click me" not in page(cfg)
    assert "file:///C:/secret" not in cfg["output"].read_text()


def test_story_without_safe_link_is_plain_text():
    from datetime import timezone
    from zoneinfo import ZoneInfo
    html = ui.render_story({"title": "T", "link": "javascript:x", "source": "S"},
                           ZoneInfo("America/Edmonton"), at("2026-10-08 06:00"))
    assert "<a" not in html and '<span class="story-link">T</span>' in html


def test_script_nonce_matches_policy(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    html = page(cfg)
    policy_nonce = re.search(r"script-src 'nonce-([^']+)'", html).group(1)
    assert f'<script nonce="{policy_nonce}">' in html


def test_offline_shows_banner_and_keeps_previous_edition(cfg, db):
    good = fake_fetch({"https://a.test/rss": [("Good story", "https://a.test/1", None)]})
    nb.run_due(cfg, db, at("2026-10-08 06:05"), good)
    offline = fake_fetch({}, fail={"https://a.test/rss", "https://b.test/rss"})
    nb.run_due(cfg, db, at("2026-10-08 13:05"), offline)
    html = page(cfg)
    assert "Good story" in html and 'class="banner"' in html
    assert "could not be reached" in html
    nb.run_due(cfg, db, at("2026-10-08 13:20"), good)               # back online
    assert 'class="banner"' not in page(cfg)


def test_broken_notes_file_still_updates_page(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    path = cfg["output"]
    path.write_text(path.read_text().replace("<!-- Afternoon end -->", ""))
    feeds = {"https://a.test/rss": [("Afternoon story", "https://a.test/2", None)]}
    with pytest.raises(nb.MarkerError):
        nb.run_due(cfg, db, at("2026-10-08 13:05"), fake_fetch(feeds))
    html = page(cfg)
    assert "Afternoon story" in html
    assert "Could not update news.md" in html


def test_missed_editions_explained(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 13:20"), fake_fetch({}))
    html = page(cfg)
    assert "Not produced on Wed 07 Oct at 19:00" in html
    assert "tab-skipped" in html


def test_pending_editions_and_welcome_before_first_run(cfg, db):
    nb.write_html(cfg, db, at("2026-10-08 05:00"))
    html = page(cfg)
    assert "No editions yet" in html
    assert "Next: Morning at 06:00" in html


def test_next_edition_tomorrow(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 19:05"), fake_fetch({}))
    assert "Next: Morning Fri 06:00" in page(cfg)                  # weekday, never a stale "tomorrow"

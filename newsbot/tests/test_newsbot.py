import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsbot as nb

TZ = ZoneInfo("America/Edmonton")


def at(text):
    """Calgary local time -> UTC datetime, like datetime.now(timezone.utc)."""
    return datetime.fromisoformat(text).replace(tzinfo=TZ).astimezone(timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    return {
        "tz": TZ, "output": tmp_path / "news.md", "max_items_per_subject": 10,
        "fail_threshold": 0.5,
        "editions": {"Morning": "06:00", "Afternoon": "13:00", "Evening": "19:00"},
        "subjects": {"Local": ["https://a.test/rss", "https://b.test/rss"],
                     "World": ["https://c.test/rss"]},
    }


@pytest.fixture
def db(tmp_path):
    return nb.open_db(tmp_path / "state.db")


def fake_fetch(feeds, fail=()):
    """feeds: url -> [(title, link, published_utc_or_None)]"""
    def fetch(url):
        if url in fail:
            raise ConnectionError("offline")
        entries = []
        for title, link, pub in feeds.get(url, []):
            e = {"title": title, "link": link}
            if pub:
                e["published_parsed"] = pub.timetuple()
            entries.append(e)
        return SimpleNamespace(feed={"title": url.split("/")[2]}, entries=entries)
    return fetch


# ---- which edition is due

@pytest.mark.parametrize("now, expected", [
    ("2026-10-08 05:59", "Evening"),     # before 06:00 the latest slot is last night's
    ("2026-10-08 06:00", "Morning"),
    ("2026-10-08 12:30", "Morning"),
    ("2026-10-08 13:05", "Afternoon"),
    ("2026-10-08 23:59", "Evening"),
    ("2026-11-01 06:00", "Morning"),     # DST ends at 02:00 that night
    ("2026-03-08 06:00", "Morning"),     # DST starts at 02:00 that night
])
def test_latest_slot(cfg, now, expected):
    slot, edition = nb.recent_slots(at(now), cfg)[-1]
    assert edition == expected
    assert slot.astimezone(TZ).hour == int(cfg["editions"][expected][:2])


def test_each_edition_appears_once_in_24h(cfg):
    assert [e for _, e in nb.recent_slots(at("2026-10-08 13:05"), cfg)] == ["Evening", "Morning", "Afternoon"]


# ---- run-due behaviour

def test_runs_once_per_slot(cfg, db):
    fetch = fake_fetch({"https://a.test/rss": [("Council approves bridge", "https://a.test/1", None)]})
    assert nb.run_due(cfg, db, at("2026-10-08 06:05"), fetch)
    assert not nb.run_due(cfg, db, at("2026-10-08 06:20"), fetch)  # already done
    assert "Council approves bridge" in cfg["output"].read_text()


def test_woke_late_gets_latest_edition_and_stubs_missed_ones(cfg, db):
    fetch = fake_fetch({})
    nb.run_due(cfg, db, at("2026-10-07 13:05"), fetch)            # Afternoon ran yesterday
    assert nb.run_due(cfg, db, at("2026-10-08 13:20"), fetch)     # laptop off since then
    text = cfg["output"].read_text()
    assert "## Afternoon — Thu 08 Oct 2026, 13:20" in text
    assert "Not produced on Wed 07 Oct at 19:00" in text          # Evening
    assert "Not produced on Thu 08 Oct at 06:00" in text          # Morning
    assert nb.run_status(db, nb.recent_slots(at("2026-10-08 13:20"), cfg)[0][0], "Evening") == "skipped"


def test_offline_keeps_previous_edition_and_retries(cfg, db):
    good = fake_fetch({"https://a.test/rss": [("Good story", "https://a.test/1", None)]})
    nb.run_due(cfg, db, at("2026-10-07 06:05"), good)
    offline = fake_fetch({}, fail={"https://a.test/rss", "https://b.test/rss"})
    assert not nb.run_due(cfg, db, at("2026-10-08 06:05"), offline)
    assert "Good story" in cfg["output"].read_text()               # not wiped
    assert nb.run_due(cfg, db, at("2026-10-08 06:20"), good)       # next check-in retries


def test_items_shown_once_and_never_older_than_24h(cfg, db):
    old = at("2026-10-06 09:00")
    feeds = {"https://a.test/rss": [("Fresh", "https://a.test/1", None),
                                    ("Two days old", "https://a.test/2", old)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    morning = cfg["output"].read_text()
    assert "Fresh" in morning and "Two days old" not in morning
    nb.run_due(cfg, db, at("2026-10-08 13:05"), fake_fetch(feeds))
    afternoon = cfg["output"].read_text().split("<!-- Afternoon start -->")[1]
    assert "Fresh" not in afternoon.split("<!-- Afternoon end -->")[0]


def test_sources_take_turns(cfg, db):
    cfg["max_items_per_subject"] = 4
    feeds = {"https://a.test/rss": [(f"A{i}", f"https://a.test/{i}", None) for i in range(10)],
             "https://b.test/rss": [("B0", "https://b.test/0", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    assert "B0" in cfg["output"].read_text()


def test_same_headline_from_two_outlets_shown_once(cfg, db):
    feeds = {"https://a.test/rss": [("Budget passes", "https://a.test/1", None)],
             "https://c.test/rss": [("Budget passes", "https://c.test/9", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    assert cfg["output"].read_text().count("Budget passes") == 1


# ---- the editable file

def test_user_notes_survive(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    path = cfg["output"]
    path.write_text("MY NOTES: call landlord\n\n" + path.read_text())
    nb.run_due(cfg, db, at("2026-10-08 13:05"), fake_fetch({}))
    assert path.read_text().startswith("MY NOTES: call landlord")


def test_broken_marker_refuses_to_write(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    path = cfg["output"]
    broken = path.read_text().replace("<!-- Afternoon end -->", "") + "\nMY NOTES\n"
    path.write_text(broken)
    with pytest.raises(nb.MarkerError):
        nb.run_due(cfg, db, at("2026-10-08 13:05"), fake_fetch({}))
    assert path.read_text() == broken                               # untouched


def test_legacy_encoding_is_read(cfg, db):
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch({}))
    path = cfg["output"]
    path.write_bytes(path.read_text().encode("utf-8") + "Notes: café".encode("cp1252"))
    nb.run_due(cfg, db, at("2026-10-08 13:05"), fake_fetch({}))
    assert "Notes: café" in path.read_text(encoding="utf-8")


def test_feed_text_cannot_inject_markers(cfg, db):
    feeds = {"https://a.test/rss": [("Odd <!-- Evening start --> [title]\n## Fake", "https://a.test/1", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    text = cfg["output"].read_text()
    nb.check_markers(text, cfg["editions"])                         # still valid
    assert r"\[title\] ## Fake" in text


# ---- headline cleaning

@pytest.mark.parametrize("raw, clean", [
    ("BREAKING: Council approves bridge", "Council approves bridge"),
    ("BREAKING NEWS: Council approves bridge", "Council approves bridge"),
    ("Just in — Council approves bridge", "Council approves bridge"),
    ("Watch: Mayor speaks", "Mayor speaks"),
    ("Breaking-ground research on batteries", "Breaking-ground research on batteries"),
    ("Watch-maker Swatch cuts jobs", "Watch-maker Swatch cuts jobs"),
    ("Yahoo! earnings beat forecast", "Yahoo! earnings beat forecast"),
    ("You won't believe this!!!", "You won't believe this"),
])
def test_clean_title(raw, clean):
    assert nb.clean_title(raw) == clean

"""News digest bot: a one-shot run, triggered every 15 minutes by Windows Task Scheduler.

    python newsbot.py run-due        what the scheduler calls: produce the edition that is due, if any
    python newsbot.py run Morning    produce one edition right now (for testing)
    python newsbot.py status         show recent runs and the next edition
    python newsbot.py open           open the reading page (news.html) in your browser
    python newsbot.py serve          share the pages on your home network (e.g. a Pi kiosk)

Each run checks a small SQLite log ("ledger") of which editions were already produced, so a
laptop that was asleep at 06:00 still gets its Morning edition within 15 minutes of waking.
"""
import argparse
import io
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import time
import tomllib
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import ui

APP_DIR = Path(__file__).resolve().parent  # never rely on the current directory
DATA_DIR = APP_DIR / "data"
WINDOW = timedelta(hours=24)
log = logging.getLogger("newsbot")


# ---------------------------------------------------------------- config

def load_config(path=APP_DIR / "config.toml"):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["tz"] = ZoneInfo(cfg["timezone"])
    cfg["output"] = (APP_DIR / cfg["output"]).resolve()  # resolve() also follows symlinks
    html_out = cfg.get("html_output")
    cfg["html_output"] = (APP_DIR / html_out).resolve() if html_out else cfg["output"].with_suffix(".html")
    cfg["json_output"] = cfg["html_output"].with_suffix(".json")  # data for the kiosk view
    return cfg


def source_name(cfg, url, feed_title=""):
    """Short name from [sources] in config.toml, else the feed's own title, else its host."""
    return cfg.get("sources", {}).get(url) or feed_title or urlparse(url).hostname or url


# ---------------------------------------------------------------- scheduling

def recent_slots(now, cfg):
    """Edition slots in the last 24 h, oldest first: [(slot_datetime, edition), ...]."""
    tz, local = cfg["tz"], now.astimezone(cfg["tz"])
    slots = []
    for day in (local.date() - timedelta(days=1), local.date()):
        for name, hhmm in cfg["editions"].items():
            h, m = map(int, hhmm.split(":"))
            slot = datetime.combine(day, dtime(h, m), tzinfo=tz)
            if local - WINDOW < slot <= local:
                slots.append((slot, name))
    return sorted(slots)


def next_slot(now, cfg):
    """The next (slot_datetime, edition) after `now`."""
    tz, local = cfg["tz"], now.astimezone(cfg["tz"])
    for day in (local.date(), local.date() + timedelta(days=1)):
        for name, hhmm in sorted(cfg["editions"].items(),
                                 key=lambda kv: tuple(map(int, kv[1].split(":")))):
            h, m = map(int, hhmm.split(":"))
            slot = datetime.combine(day, dtime(h, m), tzinfo=tz)
            if slot > local:
                return slot, name
    return None


def slot_key(slot):
    return slot.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- storage

def open_db(path=None):
    DATA_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(path or DATA_DIR / "state.db")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            slot TEXT, edition TEXT, status TEXT, at TEXT, PRIMARY KEY (slot, edition));
        CREATE TABLE IF NOT EXISTS items (
            link TEXT PRIMARY KEY, title TEXT, source TEXT, subject TEXT,
            published TEXT, first_seen TEXT);
        CREATE TABLE IF NOT EXISTS editions (
            edition TEXT PRIMARY KEY, kind TEXT, slot TEXT, produced_at TEXT, since TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS feeds (
            url TEXT PRIMARY KEY, first_checked TEXT, last_ok TEXT, last_new TEXT,
            fails INTEGER DEFAULT 0, last_error TEXT, etag TEXT, modified TEXT);
    """)
    return db


def run_status(db, slot, edition):
    row = db.execute("SELECT status FROM runs WHERE slot=? AND edition=?",
                     (slot_key(slot), edition)).fetchone()
    return row[0] if row else None


def record_run(db, slot, edition, status, now):
    db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
               (slot_key(slot), edition, status, now.isoformat()))
    db.commit()


def last_ok_run(db):
    row = db.execute("SELECT max(at) FROM runs WHERE status='ok'").fetchone()
    return datetime.fromisoformat(row[0]) if row[0] else None


def store_edition(db, edition, kind, slot, now, since=None, data=None):
    """Keep what each edition showed, so the reading page can be rebuilt at any time."""
    db.execute("INSERT OR REPLACE INTO editions VALUES (?,?,?,?,?,?)",
               (edition, kind, slot.isoformat(), now.isoformat(),
                since.isoformat() if since else None, json.dumps(data or {})))
    db.commit()


def set_problem(db, now, message):
    db.execute("INSERT OR REPLACE INTO meta VALUES ('problem', ?)",
               (json.dumps({"at": now.isoformat(), "message": message}),))
    db.commit()


def clear_problem(db):
    db.execute("DELETE FROM meta WHERE key='problem'")
    db.commit()


def get_problem(db):
    row = db.execute("SELECT value FROM meta WHERE key='problem'").fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------- fetching

CLICKBAIT = re.compile(r"^(?:breaking news|breaking|just in|watch|shocking)\s*(?::|\s[-–—|])\s*", re.I)


def clean_link(link):
    """A safe, absolute http(s) link, or None.

    Percent-encoding spaces, brackets and angle brackets means a feed can't end a Markdown
    link early or smuggle an edition marker like <!-- Morning end --> into news.md.
    """
    link = quote((link or "").strip(), safe=":/?#[]@!$&'*+,;=%~")
    return link if ui.safe_url(link) else None


def clean_title(title):
    """Tidy a headline without changing its meaning: one line, no clickbait prefix, no '!!!'."""
    while "<!--" in title or "-->" in title:  # repeat: "<!<!---- x ---->>" hides a marker
        title = title.replace("<!--", "").replace("-->", "")
    title = " ".join(title.split())
    title = CLICKBAIT.sub("", title)
    return re.sub(r"!{2,}", "", title).strip()


BOT_UA = "newsbot/1.0 (personal news digest)"
# Some sites (CBC, for one) silently stall requests that don't look like a browser.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


NOT_MODIFIED = SimpleNamespace(feed={}, entries=[], not_modified=True)


def make_fetcher(validators=None):
    """validators: {url: (etag, last_modified)} from the previous run. A site that says
    "not modified" (HTTP 304) sends nothing, so checking an unchanged feed costs almost nothing.
    The updated validators are left in fetch.validators."""
    validators = dict(validators or {})
    session = requests.Session()
    # read=0: a stalled server isn't retried with the same request; see the fallback below
    retry = Retry(total=2, read=0, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))

    def get(url, user_agent):
        headers = {"User-Agent": user_agent}
        etag, modified = validators.get(url, (None, None))
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        resp = session.get(url, timeout=(5, 20), headers=headers)
        resp.raise_for_status()
        return resp

    def fetch(url):
        try:
            resp = get(url, BOT_UA)
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            if isinstance(exc, requests.HTTPError) and exc.response.status_code != 403:
                raise
            log.info("Retrying %s as a browser (%s)", url, type(exc).__name__)
            resp = get(url, BROWSER_UA)
        if resp.status_code == 304:
            return NOT_MODIFIED
        # content-location (lowercase) lets feedparser turn relative links into absolute ones
        headers = {**{k.lower(): v for k, v in resp.headers.items()}, "content-location": resp.url}
        feed = feedparser.parse(io.BytesIO(resp.content), response_headers=headers)
        if feed.bozo and not feed.entries:
            raise ValueError(f"not a feed: {feed.get('bozo_exception')}")
        if url in validators or resp.headers.get("ETag") or resp.headers.get("Last-Modified"):
            validators[url] = (resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
        return feed
    fetch.validators = validators
    return fetch


def news_fetcher(cfg, db):
    """A fetcher primed with what each news feed said last time. Warning feeds are always
    fetched in full: an unchanged warning is still a warning."""
    news = {u for urls in cfg["subjects"].values() for u in urls}
    rows = db.execute("SELECT url, etag, modified FROM feeds WHERE etag IS NOT NULL OR modified IS NOT NULL")
    return make_fetcher({url: (etag, modified) for url, etag, modified in rows if url in news})


def wait_for_network(host, limit=120):
    """Right after the laptop wakes, Wi-Fi may not be up yet; wait up to `limit` seconds."""
    deadline = time.monotonic() + limit
    while True:
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            if time.monotonic() > deadline:
                return False
            time.sleep(10)


def collect(cfg, db, fetch, now):
    """Fetch every feed into the items table. Returns the list of feeds that failed."""
    jobs = [(subject, url) for subject, urls in cfg["subjects"].items() for url in urls]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda job: _try_fetch(fetch, job[1]), jobs))
    failed = []
    for (subject, url), (feed, error) in zip(jobs, results):
        if error:
            log.warning("Feed failed: %s (%s)", url, error)
            failed.append(url)
            record_health(db, url, now, error=error)
            continue
        source = source_name(cfg, url, clean_title(feed.feed.get("title", "")))
        new = 0
        for e in feed.entries:
            try:
                title, link = clean_title(e.get("title", "")), clean_link(e.get("link"))
                if not title or not link:
                    continue
                new += db.execute("INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?)",
                                  (link, title, source, subject, entry_time(e, now), now.isoformat())).rowcount
            except Exception as exc:  # one odd entry must not sink the edition
                log.warning("Skipped an entry in %s: %s", url, exc)
        record_health(db, url, now, new=new)
    for url, (etag, modified) in getattr(fetch, "validators", {}).items():
        db.execute("UPDATE feeds SET etag=?, modified=? WHERE url=?", (etag, modified, url))
    db.execute("DELETE FROM items WHERE first_seen < ?", ((now - 3 * WINDOW).isoformat(),))
    db.commit()
    return failed


def entry_time(entry, now):
    """Publication time as ISO text (feedparser gives UTC), or None if missing or nonsense
    (placeholders like 1 Jan 0001, or dates in the future)."""
    ts = entry.get("published_parsed") or entry.get("updated_parsed")
    try:
        when = datetime(*ts[:6], tzinfo=timezone.utc) if ts else None
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None or when.year < 2000 or when > now + timedelta(days=1):
        return None
    return when.isoformat()


def short_error(error):
    """'HTTP 404' / 'ConnectTimeout': enough to see what's wrong, nothing private."""
    response = getattr(error, "response", None)
    return f"HTTP {response.status_code}" if response is not None else type(error).__name__


def record_health(db, url, now, new=0, error=None):
    db.execute("INSERT OR IGNORE INTO feeds (url, first_checked) VALUES (?, ?)", (url, now.isoformat()))
    if error is not None:
        db.execute("UPDATE feeds SET fails = fails + 1, last_error = ? WHERE url = ?", (short_error(error), url))
    else:
        db.execute("UPDATE feeds SET fails = 0, last_ok = ? WHERE url = ?", (now.isoformat(), url))
        if new:
            db.execute("UPDATE feeds SET last_new = ? WHERE url = ?", (now.isoformat(), url))


def feed_health(cfg, db, now):
    """Feeds worth a look: failing several runs in a row, or answering but with nothing new
    for a long time (a feed that quietly stopped updating). [{name, subject, problem}]"""
    quiet_after = timedelta(hours=cfg.get("quiet_after_hours", 48))
    fail_after = cfg.get("fail_after_runs", 3)
    rows = {r[0]: r for r in db.execute("SELECT url, first_checked, last_new, fails, last_error FROM feeds")}
    found = []
    for subject, urls in cfg["subjects"].items():
        for url in urls:
            if url not in rows:
                continue
            _, first, last_new, fails, error = rows[url]
            if fails >= fail_after:
                problem = f"no answer the last {fails} times ({error})"
            elif now - datetime.fromisoformat(last_new or first) > quiet_after:
                since = datetime.fromisoformat(last_new).astimezone(cfg["tz"]) if last_new else None
                problem = (f"no new stories since {since:%a %d %b}" if since
                           else f"no stories at all in {quiet_after.days} days")
            else:
                continue
            found.append({"name": source_name(cfg, url), "subject": subject, "problem": problem})
    return found


# Environment Canada lists "No watches or warnings in effect" or "... WARNING ENDED" too.
ALL_CLEAR = re.compile(r"^no\b.*\bin effect\b|\bended\b", re.I)
MAX_ALERTS = 5


def check_alerts(cfg, fetch, now):
    """Current warnings from the [alerts] feeds. Unlike news, these are shown in every
    edition for as long as they are in effect, not just when they first appear."""
    groups = []
    for name, urls in cfg.get("alerts", {}).items():
        items, failed = [], False
        for url in urls:
            feed, error = _try_fetch(fetch, url)
            if error or not feed.entries:  # a working warnings feed always says *something*
                log.warning("Alert feed failed: %s (%s)", url, error or "no entries")
                failed = True
                continue
            for e in feed.entries[:MAX_ALERTS]:
                title = clean_title(e.get("title", ""))
                if title and not ALL_CLEAR.search(title):
                    items.append({"title": title, "link": clean_link(e.get("link")),
                                  "source": source_name(cfg, url, clean_title(feed.feed.get("title", ""))),
                                  "time": entry_time(e, now)})
        groups.append({"name": name, "items": items, "failed": failed})
    return {"checked": now.isoformat(), "groups": groups}


def _try_fetch(fetch, url):
    try:
        return fetch(url), None
    except Exception as exc:  # one bad feed must not sink the edition
        return None, exc


# ---------------------------------------------------------------- composing

def select_items(cfg, db, since, now):
    """New items (first seen after `since`, published within 24 h), grouped by subject.

    Within a subject, sources take turns so one busy feed can't crowd out the others.
    """
    rows = db.execute(
        "SELECT subject, source, title, link, coalesce(published, first_seen) FROM items "
        "WHERE first_seen > ? AND (published IS NULL OR published >= ?) "
        "ORDER BY coalesce(published, first_seen) DESC",
        (since.isoformat(), (now - WINDOW).isoformat())).fetchall()
    limit = cfg.get("max_items_per_subject", 10)
    grouped, seen_titles = {}, set()
    for subject in cfg["subjects"]:
        by_source = {}
        for subj, source, title, link, when in rows:
            if subj == subject and title.lower() not in seen_titles:
                seen_titles.add(title.lower())  # same headline from two outlets/subjects
                by_source.setdefault(source, []).append((title, link, source, when))
        queues, picked = list(by_source.values()), []
        while len(picked) < limit and any(queues):
            for q in queues:
                if q and len(picked) < limit:
                    picked.append(q.pop(0))
        dropped = sum(len(q) for q in queues)
        if dropped:
            log.info("%s: %d more items not shown (limit %d)", subject, dropped, limit)
        grouped[subject] = picked
    return grouped


def md_escape(text):
    """Headlines stay plain text in Markdown: no links, autolinks or HTML from feed text."""
    return (text.replace("\\", "\\\\").replace("<", "&lt;")
                .replace("[", r"\[").replace("]", r"\]"))


def missing_note(missing):
    """'CBC Calgary (Calgary & Alberta), BBC (World, Science & Technology)'"""
    by_name = {}
    for m in missing:
        by_name.setdefault(m["name"], []).append(m["subject"])
    return ", ".join(f"{name} ({', '.join(dict.fromkeys(subjects))})" for name, subjects in by_name.items())


def render_edition(cfg, edition, now, since, grouped, missing, alerts, health=()):
    local = now.astimezone(cfg["tz"])
    count = sum(len(v) for v in grouped.values())
    lines = [f"## {edition} — {local:%a %d %b %Y, %H:%M}",
             f"_{count} new items since {since.astimezone(cfg['tz']):%a %H:%M}_"]
    if missing:
        lines.append(f"_Missing this time: {md_escape(missing_note(missing))}_")
    for h in health:
        lines.append(f"_Source check: {md_escape(h['name'])} ({md_escape(h['subject'])}): {h['problem']}_")
    for group in alerts["groups"]:
        name = md_escape(group["name"])
        if group["items"]:
            lines.append(f"\n**{name} warnings in effect:**")
            lines += [f"- {md_escape(a['title'])}" + (f" — [{md_escape(a['source'])}]({a['link']})"
                                                       if a["link"] else "") for a in group["items"]]
        elif group["failed"]:
            lines.append(f"\n**{name}:** could not be checked this time")
        else:
            lines.append(f"\n**{name}:** no warnings in effect")
    for subject, items in grouped.items():
        lines.append(f"\n### {subject}")
        lines += [f"- {md_escape(t)} — [{md_escape(src)}]({link})" for t, link, src, _ in items]
        if not items:
            lines.append("- _No new items_")
    return "\n".join(lines)


# ---------------------------------------------------------------- the editable file

class MarkerError(Exception):
    pass


def markers(edition):
    return f"<!-- {edition} start -->", f"<!-- {edition} end -->"


def read_text(path):
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # saved by an editor in the Windows legacy encoding
        return raw.decode("cp1252")


def check_markers(text, editions):
    """Every edition needs exactly one start and one end marker, in order, with no overlap."""
    spans = []
    for name in editions:
        start, end = markers(name)
        n_start, n_end = text.count(start), text.count(end)
        if n_start != 1 or n_end != 1 or text.index(start) > text.index(end):
            raise MarkerError(f"{name}: expected one '{start}' followed by one '{end}', "
                              f"found {n_start} start / {n_end} end")
        spans.append((text.index(start), text.index(end), name))
    spans.sort()
    for (_, end_a, a), (start_b, _, b) in zip(spans, spans[1:]):
        if start_b < end_a:
            raise MarkerError(f"sections {a} and {b} overlap")


def write_sections(path, sections, editions):
    """Replace only the given editions' blocks; everything else in the file is left as is."""
    if path.exists():
        text = read_text(path)
    else:
        text = "\n\n".join(f"{markers(e)[0]}\n## {e}\n_(pending)_\n{markers(e)[1]}"
                           for e in editions) + "\n"
    for name in editions:  # an edition added to config.toml later gets a new block
        start, end = markers(name)
        if start not in text and end not in text:
            text += f"\n{start}\n## {name}\n_(pending)_\n{end}\n"
    check_markers(text, editions)
    spans = sorted(((text.index(markers(name)[0]) + len(markers(name)[0]),
                     text.index(markers(name)[1]), body) for name, body in sections.items()),
                   reverse=True)
    for i, j, body in spans:  # last block first, so earlier positions stay valid
        text = text[:i] + "\n" + body + "\n" + text[j:]
    check_markers(text, editions)  # never write a file whose structure we just broke
    atomic_write(path, text)


def atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)  # the file is never seen half-written
            return
        except PermissionError:  # Windows: another program has the file locked
            if attempt == 4:
                raise
            time.sleep(2)


# ---------------------------------------------------------------- the reading page

def write_html(cfg, db, now):
    """Rebuild news.html, and news.json for the kiosk view, from the stored editions.
    A failure here never blocks an edition."""
    try:
        stored = {row[0]: row for row in db.execute("SELECT * FROM editions")}
        slots = {name: slot for slot, name in recent_slots(now, cfg)}
        editions = []
        for name, hhmm in cfg["editions"].items():
            _, kind, slot, produced_at, since, data = stored.get(name) or (name, "pending", None, None, None, "{}")
            if name not in stored and name in slots and run_status(db, slots[name], name) == "ok":
                kind = "legacy"  # made before the reading page existed; it's in news.md
                produced_at = db.execute("SELECT at FROM runs WHERE slot=? AND edition=?",
                                         (slot_key(slots[name]), name)).fetchone()[0]
            editions.append({"name": name, "time": hhmm, "kind": kind,
                             "slot": ui.parse_time(slot), "produced_at": ui.parse_time(produced_at),
                             "since": ui.parse_time(since), **json.loads(data)})
        first_run = db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        upcoming, problem = next_slot(now, cfg), get_problem(db)
    except Exception:
        log.exception("Could not read the stored editions")
        return
    # Two files, written independently: a locked news.html must not stop the TV's news.json.
    if cfg.get("json_output"):
        try:
            atomic_write(cfg["json_output"], json.dumps({
                "generated": now.isoformat(),
                "timezone": getattr(cfg["tz"], "key", None),
                "next": {"time": upcoming[0].isoformat(), "name": upcoming[1]} if upcoming else None,
                "problem": problem,
                "editions": editions,
            }, default=lambda value: value.isoformat(), ensure_ascii=False))
        except Exception:
            log.exception("Could not write %s", cfg["json_output"])
    try:
        page = ui.render_page(editions, cfg["tz"], now, upcoming, problem, cfg["output"].name, first_run)
        atomic_write(cfg["html_output"], page)
    except Exception:
        log.exception("Could not write the reading page %s", cfg["html_output"])


# ---------------------------------------------------------------- runs

def produce(cfg, db, edition, slot, now, fetch, stubs=(), scheduled=True):
    """Fetch, compose and write one edition. Returns True if the file was updated."""
    total = sum(len(urls) for urls in cfg["subjects"].values())
    failed = collect(cfg, db, fetch, now)
    if total and len(failed) / total >= cfg.get("fail_threshold", 0.5):
        log.error("%s: %d of %d feeds failed; keeping the previous edition%s", edition,
                  len(failed), total, ", will retry" if scheduled else "")
        if scheduled:  # a manual run's failure is reported on the console instead
            set_problem(db, now, f"{len(failed)} of {total} news sources could not be reached "
                                 f"(offline?). Showing the previous edition; the bot retries "
                                 f"every 15 minutes.")
            write_html(cfg, db, now)
        return False
    since = max(filter(None, [last_ok_run(db), now - WINDOW]))
    grouped = select_items(cfg, db, since, now)
    alerts = check_alerts(cfg, fetch, now)
    health = feed_health(cfg, db, now)
    subject_of = {url: subject for subject, urls in cfg["subjects"].items() for url in urls}
    missing = [{"name": source_name(cfg, url), "subject": subject_of[url]} for url in failed]
    sections = {edition: render_edition(cfg, edition, now, since, grouped, missing, alerts, health)}
    store_edition(db, edition, "ok", slot, now, since, {
        "subjects": {subject: [{"title": t, "link": link, "source": src, "time": when}
                               for t, link, src, when in items]
                     for subject, items in grouped.items()},
        "missing": missing, "total_feeds": total, "alerts": alerts, "health": health})
    for s, name in stubs:
        note = (f"Not produced on {s:%a %d %b} at {s:%H:%M}: the computer was off or asleep. "
                f"Those stories are in the {edition} edition.")
        sections[name] = f"## {name}\n_{note}_"
        store_edition(db, name, "skipped", s, now, data={"note": note})
    try:
        write_sections(cfg["output"], sections, list(cfg["editions"]))
    except Exception as exc:
        # The reading page still gets the new edition, with a note about the notes file.
        reason = exc if isinstance(exc, MarkerError) else type(exc).__name__
        set_problem(db, now, f"Could not update {cfg['output'].name}: {reason}. "
                             f"Details are in newsbot.log on the laptop.")
        write_html(cfg, db, now)
        raise
    record_run(db, slot, edition, "ok", now)
    for s, name in stubs:
        record_run(db, s, name, "skipped", now)
    clear_problem(db)
    write_html(cfg, db, now)
    log.info("%s edition written to %s", edition, cfg["output"])
    return True


def run_due(cfg, db, now, fetch=None):
    """Produce the latest due edition if it hasn't been produced yet.

    If earlier editions in the last 24 h were missed (computer off), they are not produced
    late; their sections get a short note so stale news never sits there looking current.
    """
    slots = recent_slots(now, cfg)
    if not slots:
        return False
    slot, edition = slots[-1]
    if run_status(db, slot, edition):
        if not cfg["html_output"].exists() or (cfg.get("json_output") and not cfg["json_output"].exists()):
            write_html(cfg, db, now)  # e.g. just after upgrading: make the pages now, not at 06:00
        return False  # already done: the common case, finishes in milliseconds
    if fetch is None:
        first_url = next(u for urls in cfg["subjects"].values() for u in urls)
        if not wait_for_network(urlparse(first_url).hostname):
            log.warning("No network; will retry at the next check-in")
            set_problem(db, now, f"No internet connection at {edition} time. Showing the "
                                 f"previous edition; the bot retries every 15 minutes.")
            write_html(cfg, db, now)
            return False
        fetch = news_fetcher(cfg, db)
    missed = [(s, e) for s, e in slots[:-1] if run_status(db, s, e) is None]
    return produce(cfg, db, edition, slot, now, fetch, stubs=missed)


def run_now(cfg, db, edition, now, fetch=None):
    if edition not in cfg["editions"]:
        sys.exit(f"Unknown edition {edition!r}; choose from {', '.join(cfg['editions'])}")
    slot = max((s for s, e in recent_slots(now, cfg) if e == edition), default=now)
    return produce(cfg, db, edition, slot, now, fetch or news_fetcher(cfg, db), scheduled=False)


class AlreadyRunning(Exception):
    pass


class RunLock:
    """Stops two runs overlapping. The OS releases the lock if the process dies."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.f.close()
            raise AlreadyRunning(exc) from exc
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            import msvcrt
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        self.f.close()


def setup_logging(filename="newsbot.log"):
    DATA_DIR.mkdir(exist_ok=True)
    handlers = [RotatingFileHandler(DATA_DIR / filename, maxBytes=1_000_000,
                                    backupCount=3, encoding="utf-8")]
    if sys.stderr:  # pythonw.exe (no console) has no stderr
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)  # per-retry noise; failures are logged below


def open_page(cfg, now):
    """Rebuild the reading page if no run is busy, then open it in the default browser."""
    try:
        with RunLock(DATA_DIR / "newsbot.lock"):
            write_html(cfg, open_db(), now)
    except AlreadyRunning:
        if not cfg["html_output"].exists():
            print("A news run is in progress and there is no reading page yet; try again in a minute.")
            return 1
        print("A news run is in progress; opening the page as it is.")
    if not cfg["html_output"].exists():
        print(f"Could not build the reading page; see {DATA_DIR / 'newsbot.log'}.")
        return 1
    webbrowser.open(cfg["html_output"].as_uri())
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-due")
    sub.add_parser("status")
    sub.add_parser("open")
    sub.add_parser("serve").add_argument("--port", type=int, default=None)
    sub.add_parser("run").add_argument("edition")
    args = parser.parse_args(argv)

    # The server runs all day next to the scheduled runs, so it keeps its own log file
    # (on Windows one process can't rotate a log another process has open).
    setup_logging("server.log" if args.cmd == "serve" else "newsbot.log")
    if args.cmd == "serve":
        try:  # under pythonw nobody sees a crash, so it must end up in data/server.log
            import server
            cfg = load_config()
            return server.serve(cfg, args.port or cfg.get("serve_port", 8765),
                                loader=load_config, config_path=APP_DIR / "config.toml")
        except Exception:
            log.exception("The kiosk server stopped")
            return 1
    cfg = load_config()
    error_file = cfg["output"].with_name(cfg["output"].name + ".ERROR.txt")
    now = datetime.now(timezone.utc)
    if args.cmd == "open":
        return open_page(cfg, now)
    db = None
    try:
        with RunLock(DATA_DIR / "newsbot.lock"):
            db = open_db()
            if args.cmd == "status":
                for row in db.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 9"):
                    print(*row, sep="  |  ")
                problems = feed_health(cfg, db, now)
                print("\nSources:", "all fine" if not problems else "")
                for h in problems:
                    print(f"  {h['name']} ({h['subject']}): {h['problem']}")
                return 0
            if args.cmd == "run":
                updated = run_now(cfg, db, args.edition, now)
            else:
                updated = run_due(cfg, db, now)
            if updated:
                error_file.unlink(missing_ok=True)
    except AlreadyRunning:
        log.info("Another run is in progress; skipping this check-in")
        return 0
    except Exception as exc:
        log.exception("Run failed")
        # Somewhere you'll see it: right next to the news file, and on the reading page.
        error_file.write_text(f"{datetime.now():%Y-%m-%d %H:%M}  The news bot could not update "
                              f"{cfg['output'].name}:\n\n{exc}\n\nDetails: {DATA_DIR / 'newsbot.log'}\n",
                              encoding="utf-8")
        try:
            if db is not None and (get_problem(db) or {}).get("at") != now.isoformat():
                set_problem(db, now, f"The last update failed ({type(exc).__name__}). "
                                     f"Details are in newsbot.log on the laptop.")
                write_html(cfg, db, now)
        except Exception:
            log.exception("Could not report the failure on the reading page")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

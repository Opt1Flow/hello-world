"""News digest bot: a one-shot run, triggered every 15 minutes by Windows Task Scheduler.

    python newsbot.py run-due        what the scheduler calls: produce the edition that is due, if any
    python newsbot.py run Morning    produce one edition right now (for testing)
    python newsbot.py status         show recent runs and the next edition

Each run checks a small SQLite log ("ledger") of which editions were already produced, so a
laptop that was asleep at 06:00 still gets its Morning edition within 15 minutes of waking.
"""
import argparse
import io
import logging
import os
import re
import socket
import sqlite3
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
    return cfg


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


# ---------------------------------------------------------------- fetching

CLICKBAIT = re.compile(r"^(?:breaking news|breaking|just in|watch|shocking)\s*(?::|\s[-–—|])\s*", re.I)


def clean_title(title):
    """Tidy a headline without changing its meaning: one line, no clickbait prefix, no '!!!'."""
    title = " ".join(title.replace("<!--", "").replace("-->", "").split())
    title = CLICKBAIT.sub("", title)
    return re.sub(r"!{2,}", "", title).strip()


BOT_UA = "newsbot/1.0 (personal news digest)"
# Some sites (CBC, for one) silently stall requests that don't look like a browser.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def make_fetcher():
    session = requests.Session()
    # read=0: a stalled server isn't retried with the same request; see the fallback below
    retry = Retry(total=2, read=0, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))

    def get(url, user_agent):
        resp = session.get(url, timeout=(5, 20), headers={"User-Agent": user_agent})
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
        feed = feedparser.parse(io.BytesIO(resp.content), response_headers=dict(resp.headers))
        if feed.bozo and not feed.entries:
            raise ValueError(f"not a feed: {feed.get('bozo_exception')}")
        return feed
    return fetch


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
            continue
        source = clean_title(feed.feed.get("title", "")) or urlparse(url).hostname
        for e in feed.entries:
            title, link = clean_title(e.get("title", "")), e.get("link", "")
            if not title or not link:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")  # feedparser gives UTC
            published = datetime(*ts[:6], tzinfo=timezone.utc).isoformat() if ts else None
            db.execute("INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?)",
                       (link, title, source, subject, published, now.isoformat()))
    db.execute("DELETE FROM items WHERE first_seen < ?", ((now - 3 * WINDOW).isoformat(),))
    db.commit()
    return failed


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
        "SELECT subject, source, title, link FROM items "
        "WHERE first_seen > ? AND (published IS NULL OR published >= ?) "
        "ORDER BY coalesce(published, first_seen) DESC",
        (since.isoformat(), (now - WINDOW).isoformat())).fetchall()
    limit = cfg.get("max_items_per_subject", 10)
    grouped, seen_titles = {}, set()
    for subject in cfg["subjects"]:
        by_source = {}
        for subj, source, title, link in rows:
            if subj == subject and title.lower() not in seen_titles:
                seen_titles.add(title.lower())  # same headline from two outlets/subjects
                by_source.setdefault(source, []).append((title, link, source))
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
    return text.replace("[", r"\[").replace("]", r"\]")


def render_edition(cfg, edition, now, since, grouped, failed, total_feeds):
    local = now.astimezone(cfg["tz"])
    count = sum(len(v) for v in grouped.values())
    lines = [f"## {edition} — {local:%a %d %b %Y, %H:%M}",
             f"_{count} new items since {since.astimezone(cfg['tz']):%a %H:%M}_"]
    if failed:
        lines.append(f"_{len(failed)} of {total_feeds} feeds failed: {', '.join(failed)}_")
    for subject, items in grouped.items():
        lines.append(f"\n### {subject}")
        lines += [f"- {md_escape(t)} — [{md_escape(src)}]({link})" for t, link, src in items]
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
    for name, body in sections.items():
        start, end = markers(name)
        i, j = text.index(start) + len(start), text.index(end)
        text = text[:i] + "\n" + body + "\n" + text[j:]
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


# ---------------------------------------------------------------- runs

def produce(cfg, db, edition, slot, now, fetch, stubs=()):
    """Fetch, compose and write one edition. Returns True if the file was updated."""
    total = sum(len(urls) for urls in cfg["subjects"].values())
    failed = collect(cfg, db, fetch, now)
    if total and len(failed) / total >= cfg.get("fail_threshold", 0.5):
        log.error("%s: %d of %d feeds failed; keeping the previous edition, will retry",
                  edition, len(failed), total)
        return False
    since = max(filter(None, [last_ok_run(db), now - WINDOW]))
    grouped = select_items(cfg, db, since, now)
    sections = {edition: render_edition(cfg, edition, now, since, grouped, failed, total)}
    for s, name in stubs:
        sections[name] = (f"## {name}\n_Not produced on {s:%a %d %b} at {s:%H:%M}: the computer "
                          f"was off or asleep. Those stories are in the {edition} edition._")
    write_sections(cfg["output"], sections, list(cfg["editions"]))
    record_run(db, slot, edition, "ok", now)
    for s, name in stubs:
        record_run(db, s, name, "skipped", now)
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
        return False  # already done: the common case, finishes in milliseconds
    if fetch is None:
        first_url = next(u for urls in cfg["subjects"].values() for u in urls)
        if not wait_for_network(urlparse(first_url).hostname):
            log.warning("No network; will retry at the next check-in")
            return False
        fetch = make_fetcher()
    missed = [(s, e) for s, e in slots[:-1] if run_status(db, s, e) is None]
    return produce(cfg, db, edition, slot, now, fetch, stubs=missed)


def run_now(cfg, db, edition, now, fetch=None):
    if edition not in cfg["editions"]:
        sys.exit(f"Unknown edition {edition!r}; choose from {', '.join(cfg['editions'])}")
    slot = max((s for s, e in recent_slots(now, cfg) if e == edition), default=now)
    return produce(cfg, db, edition, slot, now, fetch or make_fetcher())


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


def setup_logging():
    DATA_DIR.mkdir(exist_ok=True)
    handlers = [RotatingFileHandler(DATA_DIR / "newsbot.log", maxBytes=1_000_000,
                                    backupCount=3, encoding="utf-8")]
    if sys.stderr:  # pythonw.exe (no console) has no stderr
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)  # per-retry noise; failures are logged below


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-due")
    sub.add_parser("status")
    sub.add_parser("run").add_argument("edition")
    args = parser.parse_args(argv)

    setup_logging()
    cfg = load_config()
    error_file = cfg["output"].with_name(cfg["output"].name + ".ERROR.txt")
    now = datetime.now(timezone.utc)
    try:
        with RunLock(DATA_DIR / "newsbot.lock"):
            db = open_db()
            if args.cmd == "status":
                for row in db.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 9"):
                    print(*row, sep="  |  ")
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
        # Somewhere you'll see it: right next to the news file.
        error_file.write_text(f"{datetime.now():%Y-%m-%d %H:%M}  The news bot could not update "
                              f"{cfg['output'].name}:\n\n{exc}\n\nDetails: {DATA_DIR / 'newsbot.log'}\n",
                              encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

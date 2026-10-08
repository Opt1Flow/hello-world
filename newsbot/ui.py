"""Builds news.html: the easy-reading view of the digest.

The page is one self-contained file, so it opens offline in any browser. Python writes all
the content, so it still reads fine with JavaScript off; the small script in template.html
only adds the edition tabs, the filter box, text size, dark mode and "2 h ago" times.
"""
import html
import re
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

TEMPLATE = Path(__file__).resolve().parent / "template.html"


def esc(text):
    return html.escape(str(text), quote=True)


def safe_url(url):
    """Only ordinary web links become clickable; javascript:, file: and the like are dropped."""
    return url if urlparse(url or "").scheme in ("http", "https") else None


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def parse_time(value):
    return datetime.fromisoformat(value) if value else None


def short_time(dt, tz, ref):
    """'06:02', or 'Wed 19:40' when it isn't on the same day as `ref`."""
    local = dt.astimezone(tz)
    return f"{local:%H:%M}" if local.date() == ref.astimezone(tz).date() else f"{local:%a %H:%M}"


def long_date(dt, tz):
    local = dt.astimezone(tz)
    return f"{local:%A} {local.day} {local:%B}"


def time_tag(dt, tz, ref):
    return f'<time datetime="{esc(dt.isoformat())}">{esc(short_time(dt, tz, ref))}</time>'


def render_story(item, tz, ref):
    title, url = esc(item["title"]), safe_url(item.get("link"))
    head = (f'<a class="story-link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
            f'{title}</a>' if url else f'<span class="story-link">{title}</span>')
    when = parse_time(item.get("time"))
    meta = esc(item.get("source", "")) + (f" · {time_tag(when, tz, ref)}" if when else "")
    return f'<li class="story">{head}<div class="meta">{meta}</div></li>'


def render_edition(ed, tz, latest):
    name = ed["name"]
    attrs = (f'id="ed-{slug(name)}" class="edition" data-edition="{esc(name)}"'
             + (f' data-produced="{esc(ed["produced_at"].isoformat())}"' if ed["produced_at"] else "")
             + (" data-latest" if latest else ""))
    if ed["kind"] == "pending":
        return (f'<section {attrs}><h2>{esc(name)}</h2><p class="empty">No {esc(name)} edition '
                f'yet. It is made at {esc(ed["time"])} each day.</p></section>')
    if ed["kind"] == "skipped":
        return (f'<section {attrs}><h2>{esc(name)}</h2>'
                f'<p class="empty">{esc(ed.get("note", "Not produced."))}</p></section>')

    produced, since = ed["produced_at"], ed["since"]
    subjects = ed.get("subjects", {})
    filled = [(s, items) for s, items in subjects.items() if items]
    empty = [s for s, items in subjects.items() if not items]
    count = sum(len(items) for _, items in filled)
    out = [f'<section {attrs}>',
           f'<div class="ed-head"><h2>{esc(name)} edition</h2>',
           f'<p class="ed-sub">{esc(long_date(produced, tz))} · {time_tag(produced, tz, produced)} · '
           f'{count} {"story" if count == 1 else "stories"} since {time_tag(since, tz, produced)}</p>']
    failed = ed.get("failed") or []
    if failed:
        hosts = ", ".join(sorted({urlparse(u).hostname or u for u in failed}))
        out.append(f'<p class="note">{len(failed)} of {ed.get("total_feeds", "?")} sources '
                   f'did not respond: {esc(hosts)}</p>')
    out.append('</div>')
    if filled:
        out.append('<nav class="jump" aria-label="Subjects">' + "".join(
            f'<a href="#{slug(name)}-{slug(s)}">{esc(s)} <span class="count">{len(items)}</span></a>'
            for s, items in filled) + '</nav>')
    for s, items in filled:
        out.append(f'<section class="subject" id="{slug(name)}-{slug(s)}">'
                   f'<h3>{esc(s)} <span class="count">{len(items)}</span></h3><ol class="stories">'
                   + "".join(render_story(i, tz, produced) for i in items) + '</ol></section>')
    if not filled:
        out.append('<p class="empty">No new stories in this edition.</p>')
    elif empty:
        out.append(f'<p class="quiet">Nothing new in {esc(", ".join(empty))}.</p>')
    out.append('<p class="no-match" hidden>No headlines match your filter.</p></section>')
    return "".join(out)


def render_page(editions, tz, now, next_slot, problem, md_path):
    """editions: [{name, time, kind, produced_at, since, subjects, failed, total_feeds, note}]"""
    produced = [e for e in editions if e["kind"] == "ok"]
    latest = max(produced, key=lambda e: e["produced_at"], default=None)

    status = []
    if latest:
        status.append(f'Updated {time_tag(latest["produced_at"], tz, now)} ({esc(latest["name"])})')
    if next_slot:
        slot, name = next_slot
        day = "" if slot.astimezone(tz).date() == now.astimezone(tz).date() else "tomorrow "
        status.append(f'Next: {esc(name)} {day}at {slot.astimezone(tz):%H:%M}')
    banner = ""
    if problem:
        at = parse_time(problem.get("at"))
        banner = (f'<p class="banner" role="status"><strong>Heads up:</strong> '
                  f'{esc(problem.get("message", ""))}'
                  + (f' <span class="banner-time">({time_tag(at, tz, now)})</span>' if at else "")
                  + '</p>')

    tabs = "".join(
        f'<a class="tab tab-{e["kind"]}" href="#ed-{slug(e["name"])}" data-target="ed-{slug(e["name"])}">'
        f'{esc(e["name"])} <span class="tab-time">{esc(e["time"])}</span></a>' for e in editions)
    body = "".join(render_edition(e, tz, e is latest) for e in editions)
    if not latest:
        body = ('<p class="welcome">No editions yet. The first one is made at the next scheduled '
                'time, or run <code>newsbot.py run Morning</code> to make one now.</p>' + body)

    nonce = secrets.token_urlsafe(16)
    replacements = {
        "{{NONCE}}": nonce,
        "{{STATUS}}": " · ".join(status),
        "{{BANNER}}": banner,
        "{{TABS}}": tabs,
        "{{BODY}}": body,
        "{{MD_PATH}}": esc(md_path),
        "{{BUILT}}": esc(f"{now.astimezone(tz):%a %d %b %Y, %H:%M}"),
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    return re.sub(r"\{\{[A-Z_]+\}\}", lambda m: replacements[m.group(0)], page)

"""Builds news.html: the easy-reading view of the digest.

The page is one self-contained file, so it opens offline in any browser. Python writes all
the content, so it still reads fine with JavaScript off; the script in template.html only
adds the edition tabs, search, text size, dark mode, live "2 h ago" times and refreshing.
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
    try:
        return url if urlparse(url or "").scheme in ("http", "https") else None
    except ValueError:  # e.g. a malformed host like https://[www.cbc.ca]/
        return None


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


def headline(item):
    title, url = esc(item["title"]), safe_url(item.get("link"))
    if not url:
        return f'<span class="story-link">{title}</span>'
    return (f'<a class="story-link" href="{esc(url)}" target="_blank" '
            f'rel="noopener noreferrer">{title}</a>')


def render_story(item, tz, ref):
    when = parse_time(item.get("time"))
    meta = esc(item.get("source", "")) + (f" · {time_tag(when, tz, ref)}" if when else "")
    return f'<li class="story">{headline(item)}<div class="meta">{meta}</div></li>'


def render_alerts(alerts, tz, ref):
    """Warnings first: a coloured box while one is in effect, one quiet line otherwise."""
    out = []
    checked = parse_time((alerts or {}).get("checked"))
    for group in (alerts or {}).get("groups", []):
        name, items = group["name"], group["items"]
        if items:
            rows = "".join(
                f'<li>{headline(a)}<div class="meta">{esc(a.get("source", ""))}'
                + (f' · issued {time_tag(parse_time(a["time"]), tz, ref)}' if a.get("time") else "")
                + '</div></li>' for a in items)
            out.append(f'<section class="alert" aria-label="{esc(name)} warnings">'
                       f'<h3>{esc(name)} {"warning" if len(items) == 1 else "warnings"} in effect</h3>'
                       f'<ul>{rows}</ul></section>')
        elif group.get("failed"):
            out.append(f'<p class="alert-quiet">{esc(name)} warnings could not be checked this time.</p>')
        else:
            out.append(f'<p class="alert-quiet">{esc(name)}: no warnings in effect'
                       + (f' · checked {time_tag(checked, tz, ref)}' if checked else "") + '</p>')
    return "".join(out)


def missing_text(missing):
    """'CBC Calgary (Calgary & Alberta), BBC (World, Science & Technology)'"""
    by_name = {}
    for m in missing:
        if isinstance(m, str):  # stored by an older version: just the feed URL
            m = {"name": urlparse(m).hostname or m, "subject": None}
        by_name.setdefault(m["name"], [])
        if m["subject"] and m["subject"] not in by_name[m["name"]]:
            by_name[m["name"]].append(m["subject"])
    return ", ".join(f"{name} ({', '.join(subjects)})" if subjects else name
                     for name, subjects in by_name.items())


def render_edition(ed, tz, latest):
    name = ed["name"]
    attrs = (f'id="ed-{slug(name)}" class="edition" data-edition="{esc(name)}"'
             + (f' data-produced="{esc(ed["produced_at"].isoformat())}"' if ed["kind"] == "ok" else "")
             + (" data-latest" if latest else ""))
    if ed["kind"] == "pending":
        return (f'<section {attrs}><div class="ed-head"><h2>{esc(name)}</h2></div><p class="empty">'
                f'No {esc(name)} edition yet. It is made at {esc(ed["time"])} each day.</p></section>')
    if ed["kind"] == "legacy":
        made = ed["produced_at"]
        return (f'<section {attrs}><div class="ed-head"><h2>{esc(name)}</h2></div><p class="empty">'
                f'Made at {time_tag(made, tz, made)}, before this reading page existed. Its stories '
                f'are in your news.md file; from the next edition on they appear here too.</p></section>')
    if ed["kind"] == "skipped":
        return (f'<section {attrs}><div class="ed-head"><h2>{esc(name)}</h2></div>'
                f'<p class="empty">{esc(ed.get("note", "Not produced."))}</p></section>')

    produced, since = ed["produced_at"], ed["since"]
    subjects = ed.get("subjects", {})
    filled = [(s, items) for s, items in subjects.items() if items]
    empty = [s for s, items in subjects.items() if not items]
    count = sum(len(items) for _, items in filled)
    out = [f'<section {attrs}>',
           f'<div class="ed-head"><h2>{esc(name)} edition</h2>',
           f'<p class="ed-sub">{esc(long_date(produced, tz))} · {time_tag(produced, tz, produced)} · '
           f'{count} new {"story" if count == 1 else "stories"} since {time_tag(since, tz, produced)}</p>']
    missing = ed.get("missing") or ed.get("failed") or []
    if missing:
        out.append(f'<p class="note">Missing this time: {esc(missing_text(missing))}</p>')
    out.append('</div>')
    out.append(render_alerts(ed.get("alerts"), tz, produced))
    if filled:
        out.append('<nav class="jump" aria-label="Subjects">' + "".join(
            f'<a href="#{slug(name)}-{slug(s)}">{esc(s)} <span class="count">{len(items)}</span></a>'
            for s, items in filled) + '</nav>')
    for s, items in filled:
        out.append(f'<section class="subject" id="{slug(name)}-{slug(s)}" tabindex="-1">'
                   f'<h3>{esc(s)} <span class="count">{len(items)}</span></h3><ol class="stories">'
                   + "".join(render_story(i, tz, produced) for i in items) + '</ol></section>')
    if not filled:
        out.append('<p class="empty">No new stories in this edition.</p>')
    elif empty:
        out.append(f'<p class="quiet">Nothing new in {esc(", ".join(empty))}.</p>')
    out.append('</section>')
    return "".join(out)


def render_page(editions, tz, now, next_slot, problem, md_path, first_run=False):
    """editions: [{name, time, kind, produced_at, since, subjects, missing, alerts, note}]"""
    produced = [e for e in editions if e["kind"] == "ok"]
    latest = max(produced, key=lambda e: e["produced_at"], default=None)

    # The status line is written here and kept current by the page's script, which reads
    # the data-* times (so "2 h ago" and "edition is late" stay true while the tab is open).
    status, data = [], []
    if latest:
        status.append(f'Updated {time_tag(latest["produced_at"], tz, now)} ({esc(latest["name"])})')
        data.append(f'data-updated="{esc(latest["produced_at"].isoformat())}" '
                    f'data-updated-name="{esc(latest["name"])}"')
    if next_slot:
        slot, name = next_slot
        local = slot.astimezone(tz)
        day = "at" if local.date() == now.astimezone(tz).date() else f"{local:%a}"
        status.append(f'Next: {esc(name)} {day} {local:%H:%M}')
        data.append(f'data-next="{esc(slot.isoformat())}" data-next-name="{esc(name)}"')
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
    if first_run:
        body = ('<p class="welcome">No editions yet. The first one is made at the next scheduled '
                'time, or run <code>newsbot.py run Morning</code> to make one now.</p>' + body)

    nonce = secrets.token_urlsafe(16)
    replacements = {
        "{{NONCE}}": nonce,
        "{{TZ}}": esc(getattr(tz, "key", "") or ""),
        "{{STATUS}}": " · ".join(status),
        "{{STATUS_DATA}}": " ".join(data),
        "{{BANNER}}": banner,
        "{{TABS}}": tabs,
        "{{BODY}}": body,
        "{{MD_PATH}}": esc(md_path),
        "{{BUILT}}": esc(f"{now.astimezone(tz):%a %d %b %Y, %H:%M}"),
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    return re.sub(r"\{\{[A-Z_]+\}\}", lambda m: replacements[m.group(0)], page)

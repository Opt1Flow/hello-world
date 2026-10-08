"""Test every feed in config.toml once and report which work: python check_feeds.py

"24h" is how many items are recent enough to appear in the digest; a working feed
with 0 there is stale and should be replaced.
"""
from datetime import datetime, timedelta, timezone

from newsbot import load_config, make_fetcher

fetch = make_fetcher()
cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
cfg = load_config()
groups = {**cfg["subjects"], **{f"{name} alerts": urls for name, urls in cfg.get("alerts", {}).items()}}
for subject, urls in groups.items():
    for url in urls:
        try:
            feed = fetch(url)
        except Exception as exc:
            print(f"FAIL  {subject} | {url} | {exc}")
            continue
        dates = [datetime(*ts[:6], tzinfo=timezone.utc) for e in feed.entries
                 if (ts := e.get("published_parsed") or e.get("updated_parsed"))]
        newest = f"{max(dates):%Y-%m-%d %H:%M} UTC" if dates else "no dates"
        recent = sum(d >= cutoff for d in dates)
        # an alert feed with nothing recent just means no warnings, which is fine
        status = "OK   " if recent or not dates or subject.endswith(" alerts") else "STALE"
        print(f"{status} {len(feed.entries):3} items | {recent:3} in 24h | newest: {newest} | {subject} | {url}")

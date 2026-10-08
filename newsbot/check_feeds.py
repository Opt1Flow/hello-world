"""Test every feed in config.toml once and report which work: python check_feeds.py"""
from newsbot import load_config, make_fetcher

fetch = make_fetcher()
for subject, urls in load_config()["subjects"].items():
    for url in urls:
        try:
            feed = fetch(url)
            latest = feed.entries[0].get("published", "no date") if feed.entries else "-"
            print(f"OK    {len(feed.entries):3} items | latest: {latest} | {subject} | {url}")
        except Exception as exc:
            print(f"FAIL  {subject} | {url} | {exc}")

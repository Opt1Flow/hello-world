import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsbot as nb
from test_newsbot import at, cfg, db, fake_fetch  # noqa: F401  (pytest fixtures)

# A realistic batch: the filler gives the word weights something to compare against.
FILLER = [
    "Calgary Transit adds late-night buses on weekends",
    "Calgary police seek witnesses after downtown collision",
    "Alberta wildfire season ends with fewer fires than average",
    "Calgary Flames sign defenceman to two-year extension",
    "Province announces funding for rural Alberta clinics",
    "Calgary library launches free tool-lending program",
]


def rows(*titles):
    sources = ["CBC Calgary", "Global News", "Calgary Herald"]
    all_titles = list(titles) + FILLER
    return [("Local", sources[i % 3], t, f"https://x.test/{i}", f"2026-10-08T12:{59 - i:02d}:00+00:00")
            for i, t in enumerate(all_titles)]


def grouped_pairs(cfg, *titles):
    return [(lead[2], [o[2] for o in others]) for lead, others in nb.group_stories(cfg, rows(*titles)) if others]


def test_same_story_different_outlets_is_merged(cfg):
    pairs = grouped_pairs(cfg, "Calgary city council approves 2027 budget",
                          "Calgary council approves budget for 2027")
    assert pairs == [("Calgary city council approves 2027 budget", ["Calgary council approves budget for 2027"])]


def test_different_neighbourhoods_stay_apart(cfg):
    assert grouped_pairs(cfg, "Calgary police investigate shooting in Forest Lawn",
                         "Calgary police investigate shooting in Bowness") == []


def test_unrelated_stories_stay_apart(cfg):
    assert grouped_pairs(cfg, "Calgary council approves budget", "Calgary council rejects arena plan") == []


def test_merged_story_shows_the_others_as_also(cfg, db):
    feeds = {"https://a.test/rss": [("Calgary city council approves 2027 budget", "https://a.test/1", None)],
             "https://b.test/rss": [("Calgary council approves budget for 2027", "https://b.test/1", None),
                                    ("Fog advisory lifted for Calgary", "https://b.test/2", None)]}
    cfg["sources"] = {"https://a.test/rss": "CBC Calgary", "https://b.test/rss": "Global News"}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    md = cfg["output"].read_text()
    assert md.count("approves") == 1 and "· also [" in md
    html = cfg["html_output"].read_text()
    assert " · also <a href=" in html
    local = json.loads(cfg["json_output"].read_text())["editions"]
    story = next(e for e in local if e["name"] == "Morning")["subjects"]["Local"][0]
    assert len(story["also"]) == 1


def ollama(vectors_by_title):
    """A stand-in for Ollama's /api/embed that returns given vectors."""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = {"embeddings": [vectors_by_title.get(t, [0.0, 0.0, 1.0]) for t in body["input"]]}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_local_ai_catches_rewording(cfg):
    a, b = "Council OKs $2B Calgary budget", "Calgary city council approves budget"
    assert grouped_pairs(cfg, a, b) == []                       # words alone: too different
    srv = ollama({a: [1.0, 0.1, 0.0], b: [0.98, 0.12, 0.0]})
    cfg["ai"] = {"ollama": f"http://127.0.0.1:{srv.server_port}", "embed_model": "nomic-embed-text"}
    try:
        assert grouped_pairs(cfg, a, b) == [(a, [b])]
    finally:
        srv.shutdown()


def test_local_ai_alone_cannot_merge_stories_with_nothing_in_common(cfg):
    a, b = "Calgary police investigate shooting in Forest Lawn", "Calgary police investigate shooting in Bowness"
    srv = ollama({a: [1.0, 0.0, 0.0], b: [0.99, 0.05, 0.0]})        # the AI thinks they're alike
    cfg["ai"] = {"ollama": f"http://127.0.0.1:{srv.server_port}", "embed_model": "nomic-embed-text"}
    try:
        groups = grouped_pairs(cfg, a, b)
    finally:
        srv.shutdown()
    assert groups == []                                              # different names: never merged


def test_ai_not_running_falls_back_to_words(cfg):
    cfg["ai"] = {"ollama": "http://127.0.0.1:9", "embed_model": "nomic-embed-text"}
    assert grouped_pairs(cfg, "Calgary city council approves 2027 budget",
                         "Calgary council approves budget for 2027") != []


def test_different_teams_stay_apart(cfg):
    assert grouped_pairs(cfg, "Calgary Flames beat Edmonton Oilers in overtime",
                         "Calgary Flames beat Vancouver Canucks in overtime") == []


def test_title_case_headlines_still_merge(cfg):
    assert grouped_pairs(cfg, "Calgary City Council Approves 2027 Budget",
                         "Calgary council approves budget for 2027") != []

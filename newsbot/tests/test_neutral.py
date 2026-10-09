import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import newsbot as nb
from test_newsbot import at, cfg, db, fake_fetch  # noqa: F401  (pytest fixtures)


# ---- the check: tone may change, facts may not

@pytest.mark.parametrize("original, rewrite", [
    ("Horrific crash on Deerfoot Trail kills 2", "Crash on Deerfoot Trail kills 2"),
    ("Premier SLAMS Ottawa's 'insane' carbon tax plan", "Premier criticizes Ottawa's carbon tax plan, calls it 'insane'"),
    ("Mayor slams council over budget chaos", "Mayor criticizes council over budget disruption"),
    ("Is Calgary's housing bubble about to burst?", "Outlook for Calgary's housing market"),
])
def test_good_rewrites_pass(original, rewrite):
    assert nb.check_rewrite(original, rewrite) is None


@pytest.mark.parametrize("original, rewrite, reason", [
    ("Crash on Deerfoot Trail kills 2", "Crash on Deerfoot Trail kills 3", "numbers"),
    ("Crash on Deerfoot Trail kills 2", "Crash on Deerfoot Trail kills several", "numbers"),
    ("Mayor slams council over budget", "Mayor Gondek criticizes council over budget", "added a name"),
    ("Premier slams Ottawa over carbon tax", "Premier criticizes federal carbon tax", "dropped a name"),
    ("Council will not raise property taxes", "Council will raise property taxes", "negation"),
    ("Mayor slams council over budget", "Mayor criticizes council, calls budget 'a disgrace'", "quote"),
    ("Mayor slams council", "Mayor", "too short"),
])
def test_rewrites_that_change_facts_are_rejected(original, rewrite, reason):
    assert reason in nb.check_rewrite(original, rewrite)


def test_rule_based_cleanup():
    assert nb.rule_neutral("Horrific crash on Deerfoot Trail kills 2") == "Crash on Deerfoot Trail kills 2"
    assert nb.rule_neutral("Mayor slams council over budget chaos") == "Mayor criticizes council over budget disruption"
    assert nb.rule_neutral("Blast at chemical plant injures 3") == "Blast at chemical plant injures 3"
    assert nb.rule_neutral("Mayor SLAMS council") == "Mayor criticizes council"


# ---- with a stand-in for Ollama

def fake_ollama(answers, calls):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            items = json.loads(body["messages"][1]["content"])
            out = {"headlines": [{"id": i["id"], "neutral": answers.get(i["headline"], i["headline"])}
                                 for i in items]}
            data = json.dumps({"message": {"content": "<think></think>" + json.dumps(out)}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def ai(cfg):
    calls, answers = [], {}
    srv = fake_ollama(answers, calls)
    cfg["ai"] = {"ollama": f"http://127.0.0.1:{srv.server_port}", "rewrite_model": "qwen3:4b"}
    yield answers, calls
    srv.shutdown()


def test_every_headline_is_shown_in_factual_form(cfg, db, ai):
    answers, calls = ai
    answers["Mayor SLAMS council over budget chaos"] = "Mayor criticizes council over budget disruption"
    answers["Horrific crash kills 2"] = "Crash kills 3"                      # changes a fact
    feeds = {"https://a.test/rss": [("Mayor SLAMS council over budget chaos", "https://a.test/1", None),
                                    ("Horrific crash kills 2", "https://a.test/2", None),
                                    ("Library extends weekend hours", "https://a.test/3", None)]}
    nb.run_due(cfg, db, at("2026-10-08 06:05"), fake_fetch(feeds))
    assert calls[0]["model"] == "qwen3:4b" and calls[0]["think"] is False
    md = cfg["output"].read_text()
    assert "- Mayor criticizes council over budget disruption — " in md
    assert "original: “Mayor SLAMS council over budget chaos”" in md
    assert "- Crash kills 2 — " in md and "kills 3" not in md                # rejected -> rules
    assert "- Library extends weekend hours — " in md                        # already neutral
    html = cfg["html_output"].read_text()
    assert 'title="Original headline: Mayor SLAMS council over budget chaos"' in html
    data = json.loads(cfg["json_output"].read_text())
    story = next(e for e in data["editions"] if e["name"] == "Morning")["subjects"]["Local"]
    assert {s["title"] for s in story} == {"Mayor criticizes council over budget disruption",
                                          "Crash kills 2", "Library extends weekend hours"}


def test_rewrites_are_remembered(cfg, db, ai):
    answers, calls = ai
    answers["Mayor slams council"] = "Mayor criticizes council"
    assert nb.neutral_headlines(cfg, db, ["Mayor slams council"], at("2026-10-08 06:05")) == \
        {"Mayor slams council": "Mayor criticizes council"}
    nb.neutral_headlines(cfg, db, ["Mayor slams council"], at("2026-10-08 13:05"))
    assert len(calls) == 1


def test_ai_not_running_uses_the_rules(cfg, db):
    cfg["ai"] = {"ollama": "http://127.0.0.1:9", "rewrite_model": "qwen3:4b"}
    out = nb.neutral_headlines(cfg, db, ["Shocking fire destroys 3 homes"], at("2026-10-08 06:05"))
    assert out == {"Shocking fire destroys 3 homes": "Fire destroys 3 homes"}


def test_try_command(cfg, ai, capsys):
    answers, _ = ai
    answers["Mayor slams council"] = "Mayor criticizes council"
    nb.try_neutral(cfg, "Mayor slams council")
    out = capsys.readouterr().out
    assert "Check:      passed" in out and "Shown as:   Mayor criticizes council" in out


def test_acronyms_are_still_names():
    assert "dropped a name" in nb.check_rewrite("RCMP arrest suspect in Airdrie shooting", "Police arrest suspect in Airdrie shooting")

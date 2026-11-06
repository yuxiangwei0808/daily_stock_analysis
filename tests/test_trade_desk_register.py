"""The register of ideas (register.py) is the one place verdicts live; the methodology doc prints it."""
from pathlib import Path

from src.services.trade_desk import chart as ch
from src.services.trade_desk import register

DOC = Path(__file__).resolve().parents[1] / "docs" / "trading-methodology.md"


def test_the_methodology_doc_prints_the_register():
    text = DOC.read_text()
    block = text.split("<!-- register:start -->\n", 1)[1].split("\n<!-- register:end -->", 1)[0]
    assert block == register.markdown_tables(), "run `python -m src.services.trade_desk.register` and paste the tables"


def test_each_verdict_is_one_of_the_methodology_words_and_leads_its_summary():
    words = {register.PASS, register.NO_EDGE, register.REJECTED, register.FORWARD, register.NOT_TESTED}
    assert len({idea.id for idea in register.IDEAS}) == len(register.IDEAS)
    for idea in register.IDEAS:
        assert idea.verdict in words, idea.id
        assert idea.summary.lower().startswith(idea.verdict), idea.id
    assert {verdict for verdict, _, _ in register.BOX_VARIANTS.values()} <= {register.NO_EDGE, register.REJECTED}


def test_only_a_pass_on_every_rule_opens_discord(monkeypatch):
    assert not register.discord_ok(None) and not register.discord_ok([])
    assert not register.discord_ok(register.SCAN_RULES) and not register.discord_ok(register.BREAKOUT_RULES)
    assert not register.discord_ok(["no_such_rule"])
    monkeypatch.setattr(register, "passed", lambda rule: rule == "swing_trend")
    assert register.discord_ok(register.BREAKOUT_RULES) and not register.discord_ok(register.SCAN_RULES)


def test_labels_read_plainly_and_the_chart_grades_come_from_the_register():
    assert register.label("model_review") == "Model review of trade ideas: no edge shown: no measurable value (|t| ≤ 1)"
    assert register.label("fade_short").startswith("fade_short (long after drops): no edge shown")
    grade = ch.grade("break_retest", 1.0, False)
    assert grade["verdict"] == register.REJECTED and grade["t"] == [-2.24, -2.90] and grade["source"] == register.BOX_SOURCE

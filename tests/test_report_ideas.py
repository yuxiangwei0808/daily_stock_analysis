"""Options ideas for the strongest calls of a scheduled report run."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.services.trade_desk import report_ideas
from src.services.trade_desk.report_ideas import ReportIdeas, format_idea

T0 = datetime(2026, 9, 25, 12, 30)


def _row(i, code, score, minutes_ago=10):
    return SimpleNamespace(id=i, code=code, sentiment_score=score, operation_advice="Buy" if score > 60 else "Watch",
                           analysis_summary=f"{code} summary", created_at=T0 - timedelta(minutes=minutes_ago))


class FakeService:
    def __init__(self, rows):
        self.rows, self.jobs, self.submitted = rows, {}, []
        self.repo = SimpleNamespace(db=SimpleNamespace(get_analysis_history=lambda days, limit: list(self.rows)),
                                    advice=lambda job_id: self.jobs.get(job_id))

    def submit(self, request, source):
        job = {"id": f"job-{request.ticker}", "status": "running", "request": {"ticker": request.ticker}}
        self.jobs[job["id"]] = job
        self.submitted.append((request.ticker, request.direction, source))
        return job


def test_ideas_pick_the_strongest_calls_after_a_finished_run_and_send_one_summary(monkeypatch):
    monkeypatch.setenv("TRADE_DESK_REPORT_IDEAS", "2")
    clock = {"now": T0}
    service = FakeService([_row(1, "OLD", 90, minutes_ago=300)])
    sent = []
    ideas = ReportIdeas(service, lambda t, p, k: sent.append((t, p, k)), now=lambda: clock["now"])
    ideas.tick(True)  # first look only records where the history stands
    service.rows += [_row(10 + i, code, score) for i, (code, score) in
                     enumerate([("NVDA", 72), ("AAPL", 55), ("JPM", 30), ("MSFT", 58), ("AMD", 52)])]
    clock["now"] += timedelta(minutes=1)
    ideas.tick(True)
    assert service.submitted == [("NVDA", "bullish", "report"), ("JPM", "bearish", "report")]
    service.jobs["job-NVDA"].update(status="completed", assessment="compare", explanation="Momentum holds. More.",
        candidates=[{"title": "Bull call debit spread (swing)", "legs": [
            {"side": "buy", "right": "call", "strike": 230, "expiry": "2026-10-16T20:00:00+00:00"},
            {"side": "sell", "right": "call", "strike": 240, "expiry": "2026-10-16T20:00:00+00:00"}],
            "payoff": {"entry_debit": 310, "max_loss": 311.3, "max_gain": 688.7, "breakevens": [233.11]},
            "probability": {"probability_of_profit": 0.41}}])
    clock["now"] += timedelta(minutes=1)
    ideas.tick(True)
    assert sent == []  # JPM still running
    service.jobs["job-JPM"].update(status="completed", assessment="wait", explanation="No clean setup yet.")
    clock["now"] += timedelta(minutes=1)
    ideas.tick(True)
    [(event_type, payload, key)] = sent
    assert event_type == "options_ideas" and key == "options-ideas:14"
    assert payload["message"].split("\n") == [
        "**NVDA** (Buy · score 72) — Bull call debit spread (swing): buy 230C 10/16 / sell 240C 10/16",
        "   debit $310.00 · max loss $311.30 · max gain $688.70 · breakeven 233.11 · P(profit) 41%",
        "> Momentum holds.",
        "**JPM** (Watch · score 30) — wait",
        "> No clean setup yet.",
    ]


def test_ideas_are_off_by_default_and_skip_runs_outside_the_session(monkeypatch):
    monkeypatch.delenv("TRADE_DESK_REPORT_IDEAS", raising=False)
    assert report_ideas.ideas_per_run() == 0
    monkeypatch.setenv("TRADE_DESK_REPORT_IDEAS", "3")
    clock = {"now": T0}
    service = FakeService([])
    ideas = ReportIdeas(service, lambda *a: None, now=lambda: clock["now"])
    ideas.tick(False)
    service.rows = [_row(i, f"T{i}", 70) for i in range(1, 7)]
    clock["now"] += timedelta(minutes=1)
    ideas.tick(False)  # after the close: quotes are not tradable, the batch is skipped
    assert service.submitted == []


def test_credit_and_unbounded_trades_are_described_plainly():
    job = {"request": {"ticker": "SPY"}, "status": "completed", "assessment": "compare", "explanation": "",
           "candidates": [{"strategy": "short_put", "legs": [{"side": "sell", "right": "put", "strike": 700,
                                                              "expiry": "2026-10-16"}],
                           "payoff": {"entry_debit": -250, "max_loss": None, "max_gain": 250, "breakevens": [697.5]}}]}
    lines = format_idea(job, {"advice": "Buy", "score": 66})
    assert lines[1] == "   credit $250.00 · max loss unbounded · max gain $250.00 · breakeven 697.5"

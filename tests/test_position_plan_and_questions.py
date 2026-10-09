"""Position alerts with what to do (levels, the usual action, the report, your alerts), sent as a
Discord card, and questions about your positions answered by the model with checked levels."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.trade_desk import discord_routes as routes
from src.services.trade_desk import position_plan as plan
from src.services.trade_desk import position_questions as pq
from tests.test_broker_holdings import MIDDAY, TODAY, _monitor, repo, store  # noqa: F401  (fixtures)

LEVELS = {"low20": 205.0, "high20": 230.0, "ma50": 199.0, "atr": 5.0, "last_close": 210.0, "avg_volume": 1}


def test_reference_levels_follow_the_side_and_skip_targets_for_geared_funds():
    long = plan.reference_levels("long", 200.0, LEVELS)
    assert (long["stop"], long["target"]) == (190.0, 215.0)
    short = plan.reference_levels("short", 200.0, LEVELS)
    assert (short["stop"], short["target"]) == (210.0, 185.0)
    assert plan.reference_levels("long", 30.0, {**LEVELS, "atr": 2.0}, geared=True)["target"] is None
    assert plan.reference_levels("mixed", 200.0, LEVELS) is None and plan.reference_levels("long", 200.0, None) is None
    assert plan.levels_line(long) == "Levels: stop 190.00 (2 ATR) · target 215.00 (3 ATR)"
    assert "20-day low 195.00" in plan.levels_line(plan.reference_levels("long", 200.0, {**LEVELS, "low20": 195.0}))


def test_option_actions_depend_on_where_the_legs_are():
    spread = {"underlying": "USO", "underlying_price": 171.0, "legs": [
        {"qty": 3, "strike": 160.0, "right": "call"}, {"qty": -3, "strike": 170.0, "right": "call"}]}
    assert "short leg is in the money" in plan.option_actions("expiry", spread, None)[0]
    long_call = {"underlying": "USO", "underlying_price": 150.0, "legs": [{"qty": 1, "strike": 160.0, "right": "call"}]}
    assert plan.option_actions("expiry", long_call, None)[0].startswith("Out of the money")
    itm = {**long_call, "underlying_price": 165.0}
    today = plan.option_actions("expiry_today", itm, None)[0]
    assert today.startswith("In the money: sell to close") and "exercised automatically" in today
    ref = plan.reference_levels("long", 150.0, {**LEVELS, "atr": 2.0})
    loss = plan.option_actions("loss", long_call, ref, level=-50)
    assert loss[0] == "The common rule cuts a long option at 50% down: close it — keep it only if USO holds above 146.00."
    assert loss[1].startswith("USO levels: stop 146.00")


def test_alerts_and_report_lines():
    assert plan.alerts_line([]).startswith("Your alerts: none set")
    rules = [{"kind": "price_below", "value": 17.8, "status": "active"}, {"kind": "pnl_above", "value": 60, "status": "paused"}]
    assert plan.alerts_line(rules) == "Your alerts: at or below 17.8"
    item = {"created_at": "2026-09-24T21:00:00", "action_label": "Reduce", "stop_loss": 17.5, "target_price": 20}
    assert plan.report_line("SOFI", TODAY, lookup=lambda ticker: [item]) == "Daily report (Sep 24): Reduce · stop 17.50 · target 20.00"
    old = {**item, "created_at": "2026-09-01T21:00:00"}
    assert plan.report_line("SOFI", TODAY, lookup=lambda ticker: [old]) == ""  # too old to quote
    assert plan.report_line("SOFI", TODAY, lookup=lambda ticker: (_ for _ in ()).throw(RuntimeError())) == ""


def test_a_trend_break_alert_says_what_to_do(store):
    monitor, events = _monitor(store, report=lambda ticker, day: "Daily report (Sep 24): Reduce · stop 190.00")
    monitor._levels = {"NVDA": LEVELS}
    monitor._levels_day = TODAY
    store.add_rule({"position_key": "NVDA", "kind": "price_below", "value": 150})
    monitor.check(MIDDAY)
    [(_, payload, _)] = [event for event in events if event[1]["kind"] == "trend"]
    assert payload["message"].startswith("Broke below its 20-day low 205.00 at 200.00: NVDA shares")  # unchanged lead
    card = payload["card"]
    assert card["title"] == "Broke below its 20-day low 205.00 at 200.00" and card["detail"][0].startswith("NVDA shares")
    assert card["plan"][0] == "A close below 205.00 (the 20-day low) is the usual exit signal: exit or cut (stop 190.00)."
    assert card["plan"][1] == "Levels: stop 190.00 (2 ATR) · target 215.00 (3 ATR)"
    assert card["report"].startswith("Daily report") and card["alerts"] == "Your alerts: at or below 150"
    assert "→ A close below 205.00" in payload["message"] and payload["held"] is True
    assert payload["message"].endswith("Review in moomoo — nothing is traded automatically.")


def test_option_alerts_carry_the_usual_action(store):
    raw = store.raw()
    for row in raw["positions"]:
        row["code"] = row["code"].replace("261016", "260929")
    store.repo.set_setting("broker_holdings", raw)
    store.service.provider("live").quotes.update({
        "USO260929C160000": {"price": 10.0, "bid": 9.9, "ask": 10.1},
        "USO260929C170000": {"price": 1.0, "bid": 0.9, "ask": 1.1}, "USO": {"price": 171.0}})
    monitor, events = _monitor(store)
    monitor.check(MIDDAY)
    cards = {payload["kind"]: payload["card"] for _, payload, _ in events}
    assert cards["expiry"]["title"] == "Expires in 2 trading days"
    assert "short leg is in the money" in cards["expiry"]["plan"][0]
    assert cards["profit"]["plan"][0].startswith("Close it: the last part of the maximum profit")
    assert cards["assignment"]["plan"][0].startswith("Close or roll the short leg")


def test_a_big_move_on_something_you_hold_becomes_its_position_card(store):
    monitor, _ = _monitor(store)
    monitor._levels = {"NVDA": LEVELS}
    card = monitor.move_card("NVDA", {"kind": "day_move", "price": 190.0, "change_pct": -5.2,
                                      "message": "NVDA down 5.2% today (past -5%) at 190.00."})
    assert card["title"].startswith("NVDA down 5.2%") and card["tone"] == "danger"
    assert card["plan"][0].startswith("A big move against you: exit if it closes past your stop (stop 180.00)")
    assert any(line.startswith("NVDA shares") for line in card["detail"])
    up = monitor.move_card("NVDA", {"kind": "day_move", "price": 210.0, "change_pct": 5.0, "message": "NVDA up 5%"})
    assert up["tone"] == "success" and "take some profit near 225.00" in up["plan"][0]
    assert monitor.move_card("AAPL", {"kind": "day_move", "price": 1, "change_pct": 5, "message": "AAPL up"}) is None


def test_position_alerts_go_to_discord_as_a_card():
    payload = {"kind": "loss", "message": "Down 50% on cost: x", "card": plan.card(
        "Down 50% on cost", ["SQQQ 10/16 35C · -52.0% on cost"], ["Cut it.", "SQQQ levels: stop 31.90 (1.5 ATR)"],
        report="Daily report (Oct 7): Sell · stop 31.00", alerts="Your alerts: none set — x")}
    content, [embed] = routes.position_message("SQQQ", "Loss", "🩸", payload, "https://desk.test/trade-desk?view=holdings")
    assert content == "💼 **SQQQ** · Loss · your position"
    assert embed["author"]["name"] == "💼 YOUR POSITION · SQQQ" and embed["title"] == "🩸 Down 50% on cost"
    assert embed["color"] == 0xE5484D and embed["url"].endswith("view=holdings")
    assert embed["fields"][0] == {"name": "What to do", "value": "Cut it.\nSQQQ levels: stop 31.90 (1.5 ATR)"}
    assert embed["fields"][1] == {"name": "Daily report (Oct 7)", "value": "Sell · stop 31.00"}
    assert "nothing is traded" in embed["footer"]["text"]
    # An alert stored before cards existed still becomes one.
    _, [old] = routes.position_message("NVDA", "Profit", "💰", {"kind": "profit", "message":
                                       "Up 50% on cost: NVDA shares\nReview in moomoo — nothing is traded automatically."})
    assert old["title"] == "💰 Up 50% on cost" and old["description"] == "NVDA shares" and old["color"] == 0x30A46C


def test_the_worker_sends_holding_alerts_and_held_moves_as_cards(monkeypatch):
    from src.services.trade_desk import worker as worker_module
    sent = []
    monkeypatch.setattr(routes, "send", lambda name, text, embeds=None: sent.append((text, embeds)) or True)
    monkeypatch.setattr("src.config.get_config", lambda: SimpleNamespace(discord_webhook_url="https://example.invalid"))
    monkeypatch.setenv("TRADE_DESK_PUBLIC_URL", "https://desk.test")
    for name in ("MARKET_PULSE_ENABLED", "TRADE_OPPORTUNITIES_ENABLED", "TRADE_DESK_BROKER_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    events = _memory_repo()
    events.set_preferences({"discord_enabled": True})
    desk = worker_module.TradeDeskWorker(SimpleNamespace(repo=events, enabled=True, holdings=None, provider=lambda m: None))
    events.event("holding_alert", {"underlying": "NVDA", "kind": "loss", "message": "Down 50% on cost: NVDA shares",
                                   "card": plan.card("Down 50% on cost", ["NVDA shares"], ["Cut it."])}, "h1")
    events.event("market_move", {"underlying": "AMD", "kind": "day_move", "change_pct": -5, "held": True,
                                 "message": "AMD down 5% today", "card": {**plan.card("AMD down 5% today", [], []), "tone": "danger"}}, "m1")
    events.event("market_move", {"underlying": "PLTR", "change_pct": 3.0, "held": None, "message": "PLTR up 3.0% today"}, "m2")
    desk._deliver()
    assert [text for text, _ in sent] == ["💼 **NVDA** · Loss · your position", "💼 **AMD** · Big move · your position",
                                          "📈 **PLTR** · Big move\nPLTR up 3.0% today"]
    assert sent[0][1][0]["url"] == "https://desk.test/trade-desk?view=holdings" and sent[2][1] is None
    deliveries = [e for e in events.events() if e["event_type"] == "discord_delivery"]
    assert all(e["payload"]["success"] and e["payload"]["parts"] == 1 for e in deliveries)


def test_the_discord_sender_posts_cards_in_one_message(monkeypatch):
    from src.notification_sender.discord_sender import DiscordSender
    posted = []
    monkeypatch.setattr("requests.post", lambda url, **kw: posted.append((url, kw["json"])) or SimpleNamespace(status_code=204))
    sender = DiscordSender(SimpleNamespace(discord_webhook_url="https://discord.test/w", discord_bot_token=None,
                                           discord_main_channel_id=None, discord_max_words=2000, webhook_verify_ssl=True))
    assert sender.send_to_discord("💼 **NVDA** · Loss", embeds=[{"title": "t"}])
    [(url, body)] = posted
    assert url == "https://discord.test/w" and body["content"] == "💼 **NVDA** · Loss" and body["embeds"] == [{"title": "t"}]


# -- questions about your positions ---------------------------------------------------------------
def _bars(price=200.0, days=300):
    start = date(2025, 7, 1)
    return [{"date": (start + timedelta(days=i)).isoformat(), "open": price, "high": price + 2.5, "low": price - 2.5,
             "close": price, "volume": 1_000} for i in range(days)]


def _context(store, key=None):
    view = store.view()
    rows = pq.positions_in(view, key)
    return pq.build_context(rows, view, store.rules(), TODAY, bars=lambda tickers, period="2y": {t: _bars() for t in tickers},
                            geared=lambda ticker: ticker == "SOXS", report=lambda ticker, day: "",
                            earnings_date=lambda ticker, day: date(2026, 10, 28))


def test_the_model_sees_prices_and_percentages_but_no_sizes_or_amounts(store):
    context = _context(store)
    text = json.dumps(context)
    assert '"qty"' not in text and "10000" not in text and "total_assets" not in text and '"value"' not in text
    nvda = next(item for item in context["positions"] if item["key"] == "NVDA")
    assert nvda["exposure"] == "long" and nvda["average_cost"] == 120.0 and nvda["weight_pct_of_account"] == 10.0
    assert nvda["rule_of_thumb"] == {"stop": 190.0, "target": 215.0}
    spread = next(item for item in context["positions"] if item["type"] == "option")
    assert spread["exposure"] == "long" and [leg["side"] for leg in spread["legs"]] == ["long", "short"]
    assert context["tickers"]["SOXS"]["leveraged_or_inverse_fund"] is True
    assert context["tickers"]["SOXS"]["next_earnings"] is None  # funds have no earnings
    assert context["tickers"]["NVDA"]["levels"] == {"low20": 197.5, "high20": 202.5, "ma50": 200.0, "atr": 5.0}
    with pytest.raises(ValueError, match="no longer held"):
        pq.positions_in(store.view(), "AAPL")


def test_levels_on_the_wrong_side_are_replaced_and_unknown_positions_dropped(store):
    context = _context(store)
    answer = pq.parse_answer({"summary": "Hold NVDA.", "positions": [
        {"key": "NVDA", "action": "Take profit", "stop": 205, "stop_basis": "above?", "target": 230, "target_basis": "20-day high"},
        {"key": "USO 2026-10-16", "action": "hold", "stop": 145, "target": None, "pnl_stop_pct": -40, "pnl_target_pct": -20},
        {"key": "AAPL", "action": "close"}]}, context)
    nvda, spread = answer["positions"]
    assert nvda["action"] == "take_profit" and nvda["stop"] == 190.0 and nvda["stop_source"] == "rule"
    assert "wrong side" in nvda["stop_basis"] and (nvda["target"], nvda["target_source"]) == (230.0, "model")
    assert spread["stop"] == 145.0 and spread["target"] is None
    # The spread is at -17.8%: a cut at -40% is ahead of it, a "target" of -20% is already behind it.
    assert spread["pnl_stop_pct"] == -40.0 and spread["pnl_target_pct"] is None
    with pytest.raises(ValueError):
        pq.parse_answer({}, context)


def test_asking_saves_the_answer_and_a_failure_says_why(store):
    seen = {}

    def generate(prompt):
        seen["prompt"] = prompt
        return {"data": {"summary": "Set a stop under 192.50.", "positions": [{"key": "NVDA", "action": "hold", "stop": 192.5}]},
                "model": "test-model"}
    kwargs = dict(wait=True, bars=lambda tickers, period="2y": {t: _bars() for t in tickers},
                  report=lambda ticker, day: "", earnings_date=lambda ticker, day: None)
    item = store.ask_positions("Where should my NVDA stop be?", "NVDA", generate=generate, **kwargs)
    [saved] = store.questions()
    assert saved["id"] == item["id"] and saved["status"] == "done" and saved["model"] == "test-model"
    assert saved["positions"][0]["stop"] == 192.5 and "Question: Where should my NVDA stop be?" in seen["prompt"]
    assert '"key": "NVDA"' in seen["prompt"] and "USO" not in seen["prompt"]  # one position asked about

    def broken(prompt):
        raise RuntimeError("backend down")
    store.ask_positions("Review all", None, generate=broken, **kwargs)
    latest = store.questions()[0]
    assert latest["status"] == "failed" and "unavailable" in latest["error"]
    store.delete_question(latest["id"])
    assert [q["id"] for q in store.questions()] == [item["id"]]
    with pytest.raises(KeyError):
        store.delete_question("nope")


def test_one_question_at_a_time_and_a_restart_does_not_leave_one_running(store):
    store._save_question({"id": "a", "question": "q", "status": "running", "created_at": datetime.now(timezone.utc).isoformat()})
    with pytest.raises(ValueError, match="still being answered"):
        store.ask_positions("another", None, wait=True, generate=lambda p: {})
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    store._save_question({"id": "a", "question": "q", "status": "running", "created_at": old})
    assert store.questions()[0]["status"] == "failed" and "restarted" in store.questions()[0]["error"]


def _memory_repo():
    from contextlib import contextmanager
    import threading
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.services.trade_desk.repository import TradeDeskRepository
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions, lock = sessionmaker(engine), threading.RLock()

    @contextmanager
    def session():
        with lock, sessions() as opened:
            yield opened

    @contextmanager
    def transaction():
        with lock, sessions() as opened:
            with opened.begin():
                yield opened
    return TradeDeskRepository(SimpleNamespace(_engine=engine, get_session=session, session_scope=transaction))

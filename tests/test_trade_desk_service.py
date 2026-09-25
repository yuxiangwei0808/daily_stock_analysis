"""Trade Desk persistence and fill accounting acceptance checks."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.services.trade_desk.advisor import AdvisorError, Narrative, Selection
from src.services.trade_desk.ledger import position_from_fills
from src.services.trade_desk.models import (
    OptionLeg, OptionQuote, PayoffAnalysis, QuoteSnapshot, StrategyCandidate,
    TradeAdviceRequest, TradePlan, utcnow,
)
from src.services.trade_desk.repository import TradeDeskRepository


@pytest.fixture(autouse=True)
def untiered_config(monkeypatch):
    """Keep the user's .env model tiers from reaching live models in these tests."""
    config = SimpleNamespace(targeted_generation_backend='', second_opinion_backends=[], report_language='en',
                             generation_backend='litellm', litellm_model='openai/test-model',
                             claude_code_cli_model='claude-test')
    monkeypatch.setattr('src.config.get_config', lambda: config)
    return config


@pytest.fixture
def repo():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    sessions = sessionmaker(engine)

    @contextmanager
    def transaction():
        with sessions() as session:
            with session.begin():
                yield session

    database = SimpleNamespace(_engine=engine, get_session=sessions, session_scope=transaction,
                               get_recent_news=lambda *a, **k: [])
    yield TradeDeskRepository(database)
    engine.dispose()


def snapshot(mode='live', stale=False):
    now = utcnow()
    return QuoteSnapshot(underlying='TEST', spot=100, bid=99.99, ask=100.01,
        quoted_at=now, provider='test_verified', mode=mode, session='regular',
        source_verified=True, stale=stale, options=[OptionQuote(
            contract_id='TEST_CALL', underlying='TEST', right='call', strike=100,
            expiry=now + timedelta(days=7), expiry_verified=True, bid=2.0, ask=2.1,
            quoted_at=now, iv=0.25, bid_size=5, ask_size=5, volume=100, open_interest=1000)])


def candidate(snap):
    q = snap.options[0]
    return StrategyCandidate(strategy='long_call', title='Long call', underlying='TEST',
        horizon='swing', snapshot_id=snap.id, legs=[OptionLeg(contract_id=q.contract_id,
            right='call', side='buy', strike=100, expiry=q.expiry, entry_price=2.1, iv=0.25)],
        payoff=PayoffAnalysis(entry_debit=210, fees=0.65, max_loss=210.65,
            loss_bound='bounded', gain_bound='unbounded', capital_required=210.65))


def plan(repo, ledger='manual_live', mode='live'):
    snap = snapshot(mode)
    item = candidate(snap)
    job = repo.create_advice(TradeAdviceRequest(ticker='TEST', data_mode=mode).model_dump(mode='json'))
    repo.update_advice(job['id'], {'status': 'completed', 'candidates': [item.model_dump(mode='json')]})
    record = TradePlan(advice_id=job['id'], candidate=item, ledger=ledger, data_mode=mode)
    return repo.add_plan(record.model_dump(mode='json')), snap


class FakeProvider:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self, ticker, expiry=None, required_contracts=()):
        self.required_contracts = tuple(required_contracts)
        return self.snap

    def health(self):
        return {'available': True}

    def close(self):
        pass


class FailingAdvisor:
    def explain(self, *a, **kw):
        raise AdvisorError('test model failure')


def service(repo, snap=None, advisor=None):
    from src.services.trade_desk.service import TradeDeskService
    return TradeDeskService(repo, lambda mode: FakeProvider(snap or snapshot(mode)), advisor or FailingAdvisor())


def fill(code='TEST_CALL', side='buy', quantity=1, price=2.1, intent='open'):
    return {'contract_id': code, 'side': side, 'quantity': quantity, 'price': price,
            'fees': 0.65, 'filled_at': utcnow(), 'intent': intent}


def test_cancelled_advice_cannot_be_resurrected(repo):
    job = repo.create_advice({'ticker': 'TEST'})
    repo.update_advice(job['id'], {'status': 'cancelled'})
    assert repo.update_advice(job['id'], {'status': 'completed'}) is None
    assert repo.advice(job['id'])['status'] == 'cancelled'


def test_followups_preserve_versions(repo):
    first = repo.create_advice({'ticker': 'TEST', 'allocation': 1000})
    second = repo.create_advice({'ticker': 'TEST', 'allocation': 500, 'parent_advice_id': first['id']})
    assert first['conversation_id'] == second['conversation_id']
    assert repo.advice(first['id'])['request']['allocation'] == 1000


def test_only_one_worker_can_hold_lease(repo):
    assert repo.lease('first')
    assert not repo.lease('second')
    assert repo.lease('first')
    repo.release('first')
    assert repo.lease('second')


def test_event_replay_and_dedup_survive_restart(repo):
    first = repo.event('target', {'plan_id': 'p'}, 'same-target')
    again = TradeDeskRepository(repo.db)
    assert again.event('target', {'plan_id': 'p'}, 'same-target') is None
    second = again.event('invalidation', {'plan_id': 'p'}, 'different-event')
    assert [e['id'] for e in again.events(after=first['id'])] == [second['id']]


def test_partial_close_retains_exposure_and_real_costs(repo):
    row, snap = plan(repo)
    svc = service(repo, snap)
    svc.record_fill(row['id'], fill(quantity=2))
    result = svc.record_fill(row['id'], fill(side='sell', price=2.5, intent='close'))
    assert result['status'] == 'open'
    assert result['legs'][0]['signed_quantity'] == 1
    assert result['realized_pnl'] == pytest.approx(38.7)
    assert result['unrealized_pnl'] is None
    with pytest.raises(ValueError, match='exceeds'):
        svc.record_fill(row['id'], fill(side='sell', quantity=2, intent='close'))
    assert len(repo.fills(row['id'])) == 2
    svc.stop()


def test_replay_never_creates_live_plan(repo):
    row, snap = plan(repo, 'paper', 'replay')
    svc = service(repo, snap)
    with pytest.raises(ValueError, match='Replay'):
        svc.create_plan(row['advice_id'], row['candidate']['id'], 'manual_live')
    with pytest.raises(ValueError, match='manual-live'):
        svc.record_fill(row['id'], fill())
    svc.stop()


def test_paper_uses_ask_bid_and_checks_size(repo):
    row, snap = plan(repo, 'paper')
    svc = service(repo, snap)
    with pytest.raises(ValueError, match='limit'):
        svc.paper_fill(row['id'], limit_price=205)
    assert not repo.fills(row['id'])
    with pytest.raises(ValueError, match='displayed size'):
        svc.paper_fill(row['id'], quantity=6)
    svc.paper_fill(row['id'])
    assert repo.fills(row['id'])[0]['price'] == 2.1
    snap.options[0].bid = 2.4
    snap.options[0].ask = 2.5
    result = svc.paper_fill(row['id'], intent='close')
    assert result['status'] == 'closed'
    assert result['realized_pnl'] == pytest.approx(28.7)
    assert svc.outcomes()['paper']['closed_trades'] == 1
    assert svc.outcomes()['manual_live']['closed_trades'] == 0
    assert svc.outcomes()['paper_replay']['closed_trades'] == 0
    svc.stop()


def test_stale_quotes_block_simulation_not_fill_recording(repo):
    paper, stale = plan(repo, 'paper')
    stale.stale = True
    svc = service(repo, stale)
    with pytest.raises(ValueError, match='Fresh'):
        svc.paper_fill(paper['id'])
    live, _ = plan(repo)
    assert svc.record_fill(live['id'], fill())['status'] == 'open'
    svc.stop()


def test_expiration_requires_reconciliation(repo):
    row, _ = plan(repo)
    svc = service(repo)
    svc.record_fill(row['id'], fill())
    result = position_from_fills(row, repo.fills(row['id']), now=utcnow() + timedelta(days=8))
    assert result['status'] == 'reconciliation_required'
    assert result['legs'][0]['signed_quantity'] == 1
    svc.stop()


def test_owned_shares_are_part_of_economic_exposure(repo):
    row, snap = plan(repo)
    row['candidate']['legs'].append(OptionLeg(contract_id='TEST', right='stock', side='buy',
        quantity=100, multiplier=1, entry_price=100, existing=True).model_dump(mode='json'))
    result = position_from_fills(row, [], snapshot=snap)
    assert result['legs'][0]['signed_quantity'] == 100
    assert result['unrealized_pnl'] == pytest.approx(-1)


def test_invalid_codex_retains_labeled_calculations(repo, monkeypatch):
    from src.services.trade_desk import analytics
    snap = snapshot('replay')
    monkeypatch.setattr(analytics, 'build_candidates', lambda *args: [candidate(snap)])
    svc = service(repo, snap)
    job = repo.create_advice(TradeAdviceRequest(ticker='TEST', data_mode='replay').model_dump(mode='json'))
    svc._run_advice(job['id'], threading.Event())
    result = repo.advice(job['id'])
    assert result['status'] == 'completed'
    assert result['llm_status'] == 'unavailable'
    assert result['assessment'] == 'wait'
    assert len(result['candidates']) == 1
    svc.stop()


@pytest.mark.parametrize("mutation", ["spot", "strike"])
def test_price_change_during_model_call_invalidates_proposal(repo, monkeypatch, mutation):
    from src.services.trade_desk import analytics
    snap = snapshot()
    def calculate(market, request):
        item = candidate(market)
        item.legs[0].strike = market.options[0].strike
        return [item]
    monkeypatch.setattr(analytics, 'build_candidates', calculate)

    class MovingAdvisor:
        def explain(self, request, old, candidates, **kwargs):
            item = candidates[0]
            original = old.model_copy(deep=True)
            if mutation == "spot":
                snap.spot = 105
            else:
                snap.options[0].strike = 101
            return Narrative(assessment='compare', explanation='Conditional comparison',
                selections=[Selection(candidate_id=item.id, reason='test')]), {item.id: item}, {
                    item.id: {'snapshot': original, 'request': request}}

    svc = service(repo, snap, MovingAdvisor())
    job = repo.create_advice(TradeAdviceRequest(ticker='TEST').model_dump(mode='json'))
    svc._run_advice(job['id'], threading.Event())
    assert repo.advice(job['id'])['status'] == 'stale'
    svc.stop()


def test_api_authentication_validation_and_mode_isolation(repo, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.middlewares import auth
    from api.v1.endpoints.trade_desk import router
    row, snap = plan(repo, 'paper', 'replay')
    svc = service(repo, snap)
    app = FastAPI()
    app.state.trade_desk_service = svc
    app.include_router(router, prefix='/api/v1/trade-desk')
    auth.add_auth_middleware(app)
    monkeypatch.setattr(auth, 'is_auth_enabled', lambda: True)
    monkeypatch.setattr(auth, 'verify_session', lambda token: token == 'valid-test-session')
    with TestClient(app) as client:
        assert client.get('/api/v1/trade-desk/plans').status_code == 401
        assert client.post('/api/v1/trade-desk/advice', json={'ticker': 'TEST'}).status_code == 401
        assert client.get('/api/v1/trade-desk/events').status_code == 401
        auth_cookies = {auth.COOKIE_NAME: 'valid-test-session'}
        assert client.get('/api/v1/trade-desk/plans', cookies=auth_cookies).status_code == 200
        assert client.post('/api/v1/trade-desk/plans', cookies=auth_cookies, json={
            'advice_id': row['advice_id'], 'candidate_id': row['candidate']['id'],
            'ledger': 'manual_live'}).status_code == 422
        assert client.post('/api/v1/trade-desk/advice', cookies=auth_cookies, json={
            'ticker': 'TEST', 'allocation': -1}).status_code == 422
        assert client.patch('/api/v1/trade-desk/preferences', cookies=auth_cookies, json={
            'proactive_enabled': True, 'opportunity_daily_limit': 3}).json()['proactive_enabled'] is True
        response = client.post('/api/v1/trade-desk/plans', cookies=auth_cookies, json={
            'advice_id': row['advice_id'], 'candidate_id': row['candidate']['id']})
        assert response.status_code == 201, response.text
        assert response.json()['ledger'] == 'paper'
        assert client.patch(f"/api/v1/trade-desk/plans/{row['id']}", cookies=auth_cookies, json={'status': 'closed'}).status_code == 422
    svc.stop()


def test_monitor_alerts_do_not_create_fills(repo):
    from src.services.trade_desk.worker import TradeDeskWorker
    row, snap = plan(repo, 'paper')
    repo.update_plan(row['id'], {'trigger_price': 99, 'trigger_direction': 'above'})
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    worker.run_once()
    worker.run_once()
    triggers = [e for e in repo.events() if e['event_type'] == 'price_trigger']
    assert len(triggers) == 1
    assert not repo.fills(row['id'])
    assert svc.position(row['id'])['status'] == 'watching'
    worker.stop()
    svc.stop()


def test_discord_delivery_retries_are_bounded_and_persisted(repo, monkeypatch):
    from src.services.trade_desk import worker as module
    import src.config
    import src.notification
    row, snap = plan(repo, 'paper')
    svc = service(repo, snap)
    worker = module.TradeDeskWorker(svc)
    repo.set_preferences({'discord_enabled': True})
    repo.event('target', {'plan_id': row['id'], 'message': 'test target'}, 'target-once')
    monkeypatch.setattr(src.config, 'get_config', lambda: SimpleNamespace(discord_webhook_url='configured'))
    calls = []

    class Delivery:
        def send_to_discord(self, message):
            calls.append(message)
            return False

    monkeypatch.setattr(src.notification, 'NotificationService', Delivery)
    now = utcnow()
    for minute in range(5):
        monkeypatch.setattr(module, 'utcnow', lambda m=minute: now + timedelta(minutes=m * 2))
        worker._deliver()
    assert len(calls) == 3
    assert [e['payload']['attempt'] for e in repo.events() if e['event_type'] == 'discord_delivery'] == [1, 2, 3]
    worker.stop()
    svc.stop()


def test_proactive_requires_fresh_quotes_and_deduplicates_ideas(repo, monkeypatch):
    from src.services.trade_desk.worker import TradeDeskWorker
    import data_provider.us_session
    monkeypatch.setattr(data_provider.us_session, 'session_window', lambda now=None: ('regular', None, None))
    snap = snapshot()
    snap.evidence = [{'kind': 'market_scan', 'title': 'Observed test market context'}]
    item = candidate(snap).model_copy(update={'evidence_confidence': 'high',
        'entry_conditions': ['Break the observed range'], 'invalidation': 'Range failure',
        'exit_conditions': ['Close if invalidated']})
    job = repo.create_advice(TradeAdviceRequest(ticker='TEST').model_dump(mode='json'), source='proactive')
    repo.update_advice(job['id'], {'status': 'completed', 'llm_status': 'ready',
        'assessment': 'compare', 'explanation': 'Conditional test idea',
        'snapshot': snap.model_dump(mode='json'),
        'candidates': [item.model_dump(mode='json')]})
    repo.set_preferences({'proactive_enabled': True})
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    worker._next_scan = float('inf')
    svc._latest[('live', 'TEST')] = snap
    snap.stale = True
    worker._proactive()
    assert not [e for e in repo.events() if e['event_type'] == 'opportunity']
    snap.stale = False
    snap.options[0].ask = 4
    worker._proactive()
    assert not [e for e in repo.events() if e['event_type'] == 'opportunity']
    snap.options[0].ask = 2.1
    worker._proactive()
    worker._proactive()
    assert len([e for e in repo.events() if e['event_type'] == 'opportunity']) == 1
    worker.stop()
    svc.stop()


def test_aged_live_candidate_is_repriced_at_selection_and_refused_after_a_real_move(repo):
    row, snap = plan(repo)
    aged = snap.model_copy(deep=True)
    aged.quoted_at -= timedelta(minutes=2)
    aged.options[0].quoted_at -= timedelta(minutes=2)
    repo.update_advice(row['advice_id'], {'snapshots': {aged.id: aged.model_dump(mode='json')},
                                          'snapshot': aged.model_dump(mode='json')})
    item = row['candidate']
    item['snapshot_id'] = aged.id
    repo.update_advice(row['advice_id'], {'candidates': [item]})
    svc = service(repo, snap)  # the provider now serves fresh quotes at the same prices
    try:
        selected = svc.create_plan(row['advice_id'], item['id'])
        assert selected['candidate']['id'] == item['id']
        assert selected['candidate']['snapshot_id'] == snap.id
        assert selected['candidate']['legs'][0]['entry_price'] == pytest.approx(2.1)
        snap.options[0].ask = 3.5  # a real repricing of the position
        with pytest.raises(ValueError, match='moved materially'):
            svc.create_plan(row['advice_id'], item['id'])
    finally:
        svc.stop()


def test_stale_option_pauses_stock_trigger(repo, monkeypatch):
    import data_provider.us_session
    from src.services.trade_desk.worker import TradeDeskWorker
    monkeypatch.setattr(data_provider.us_session, 'session_window', lambda now=None: ('regular', None, None))
    row, snap = plan(repo, 'paper')
    repo.update_plan(row['id'], {'trigger_price': 99, 'trigger_direction': 'above'})
    snap.options[0].quoted_at -= timedelta(minutes=1)
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    worker.run_once()
    events = repo.events()
    assert any(event['event_type'] == 'data_outage' for event in events)
    assert not any(event['event_type'] == 'price_trigger' for event in events)
    worker.stop()
    svc.stop()


def test_assignment_keeps_resulting_shares_until_reconciled(repo):
    row, snap = plan(repo)
    svc = service(repo, snap)
    svc.record_fill(row['id'], fill())
    svc.record_fill(row['id'], fill(side='sell', price=0, intent='exercise'))
    result = svc.record_fill(row['id'], fill(code='TEST', quantity=100, price=100, intent='exercise'))
    assert result['status'] == 'reconciliation_required'
    assert result['legs'][0]['contract_id'] == 'TEST'
    assert result['legs'][0]['quantity'] == 100
    assert svc.reconcile(row['id'], 'Recorded exercise and 100 shares at strike')['status'] == 'open'
    svc.stop()


@pytest.mark.parametrize('response', [
    {'assessment': 'compare', 'explanation': 'test', 'selections': [
        {'candidate_id': 'invented-contract', 'reason': 'not supplied'}]},
    {'assessment': 'compare', 'explanation': 'test', 'selections': [], 'max_loss': 0},
    {'assessment': 'certain_win', 'explanation': 'unsupported', 'selections': []},
])
def test_codex_structured_boundary_rejects_unknown_ids_and_numeric_overrides(monkeypatch, response):
    import json
    import src.agent.codex_agent_backend
    from src.services.trade_desk.advisor import CodexTradeAdvisor
    result = SimpleNamespace(success=True, final_answer=json.dumps(response), usage=None)
    monkeypatch.setattr(src.agent.codex_agent_backend, 'CodexAgentBackend',
                        lambda *args, **kwargs: SimpleNamespace(run=lambda request: result))
    snap = snapshot()
    with pytest.raises(AdvisorError):
        CodexTradeAdvisor().explain(TradeAdviceRequest(ticker='TEST'), snap, [candidate(snap)],
                                   cancel=threading.Event())


def test_worker_restart_recovers_interrupted_jobs_without_erasing_history(repo):
    queued = repo.create_advice({'ticker': 'TEST'})
    done = repo.create_advice({'ticker': 'TEST'})
    repo.update_advice(done['id'], {'status': 'completed', 'explanation': 'Saved original'})
    TradeDeskRepository(repo.db).recover_jobs()
    assert repo.advice(queued['id'])['status'] == 'failed'
    assert repo.advice(done['id'])['explanation'] == 'Saved original'


def test_comparison_tool_preserves_scope_and_explicit_financial_inputs():
    from src.services.trade_desk.tool_worker import effective_request
    request = TradeAdviceRequest(ticker='TEST', allocation=1500, message='Compare using 500 dollars.')
    changed = effective_request(request, {'allocation': 500, 'data_mode': 'live', 'direction': 'bearish'})
    assert changed.allocation == 500
    assert request.allocation == 1500
    for changes in ({'data_mode': 'replay'}, {'ticker': 'OTHER'}, {'allocation': 999},
                    {'existing_shares': 100}, {'margin_per_unit': 100}):
        with pytest.raises(ValueError):
            effective_request(request, changes)


def test_calculation_tool_runs_in_owned_process_and_returns_new_candidates():
    import time
    from src.agent.tools.execution import ToolAccessContext
    from src.services.trade_desk.tool_worker import TradeToolRunner
    request = TradeAdviceRequest(ticker='TEST', data_mode='replay', horizon='swing',
        allocation=1500, message='Change allocation to 500 dollars and compare puts.')
    snap = snapshot(mode='replay')
    snap.options[0].right = 'put'
    received = []
    runner = TradeToolRunner(request, snap, lambda req, market, candidates: received.append((req, market, candidates)))
    try:
        result = runner.execute('compare_trade_strategies', {'changes': {
            'allocation': 500, 'data_mode': 'replay', 'direction': 'bearish', 'strategies': ['long_put']}},
            ToolAccessContext(deadline=time.monotonic() + 60, redact_result=True))
        assert result['ok'], result
        assert received[0][0].allocation == 500
        assert received[0][2][0].strategy == 'long_put'
        assert not runner.snapshot()[0]['alive_after']
        assert runner.snapshot()[0]['isolation_valid']
        result = runner.execute('compare_trade_strategies', {'changes': {'horizon': 'swing'}},
            ToolAccessContext(deadline=time.monotonic() + 60, redact_result=True))
        assert result['ok'], result
        assert received[-1][0].allocation == 500
        assert received[-1][0].direction == 'bearish'
        assert request.allocation == 1500
    finally:
        runner.close()


def test_paper_covered_calls_respect_all_explicitly_owned_shares(repo):
    snap = snapshot('replay')
    item = candidate(snap)
    item.strategy = 'covered_call'
    item.legs[0].side = 'sell'
    item.legs.append(OptionLeg(contract_id='TEST', right='stock', side='buy',
        quantity=100, multiplier=1, entry_price=100, existing=True))
    job = repo.create_advice(TradeAdviceRequest(ticker='TEST', data_mode='replay',
                            existing_shares=300).model_dump(mode='json'))
    repo.update_advice(job['id'], {'status': 'completed', 'candidates': [item.model_dump(mode='json')]})
    svc = service(repo, snap)
    selected = svc.create_plan(job['id'], item.id)
    assert selected['existing_share_quantity'] == 300
    result = svc.paper_fill(selected['id'], quantity=2)
    assert {leg['contract_id']: leg['signed_quantity'] for leg in result['legs']} == {
        'TEST': 300, 'TEST_CALL': -2}
    with pytest.raises(ValueError, match='do not cover'):
        svc.paper_fill(selected['id'], quantity=2)
    assert len(repo.fills(selected['id'])) == 1
    svc.paper_fill(selected['id'], quantity=1)
    with pytest.raises(ValueError, match='do not cover'):
        svc.paper_fill(selected['id'])
    svc.stop()


def test_discovery_carries_attributed_research_without_replacing_quotes(repo, monkeypatch):
    from src.services.trade_desk.worker import TradeDeskWorker
    import src.services.us_market_scan
    import src.config
    monkeypatch.setattr(src.config, 'get_config', lambda: SimpleNamespace(stock_list=['TEST']))
    monkeypatch.setattr(src.services.us_market_scan, 'collect_us_market_scan', lambda watchlist: {
        'session': 'regular', 'gainers': [{'code': 'OTHER', 'price': 999, 'change_pct': 3,
        'volume': 100000, 'amount': 1000000, 'provider_timestamp': '2026-09-22T14:00:00Z'}]})
    snap = snapshot()
    snap.underlying = 'OTHER'
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    assert worker._discover() == ['TEST', 'OTHER']
    market = svc.snapshot(TradeAdviceRequest(ticker='OTHER'))
    assert market.spot == 100
    assert market.evidence[0]['metrics']['price'] == 999
    assert 'research data' in market.evidence[0]['source']
    assert svc.snapshot(TradeAdviceRequest(ticker='OTHER')).evidence == market.evidence
    worker.stop()
    svc.stop()


def test_discord_attempt_claim_prevents_duplicate_delivery_from_old_worker_view(repo, monkeypatch):
    from src.services.trade_desk.worker import TradeDeskWorker
    import src.config
    import src.notification
    svc = service(repo)
    repo.set_preferences({'discord_enabled': True})
    repo.event('target', {'message': 'test'})
    old_events = repo.events(newest=True)
    monkeypatch.setattr(repo, 'events', lambda **kwargs: old_events)
    monkeypatch.setattr(src.config, 'get_config', lambda: SimpleNamespace(discord_webhook_url='configured'))
    calls = []
    monkeypatch.setattr(src.notification, 'NotificationService', lambda: SimpleNamespace(
        send_to_discord=lambda message: calls.append(message) or True))
    first, second = TradeDeskWorker(svc), TradeDeskWorker(svc)
    first._deliver()
    second._deliver()
    assert len(calls) == 1
    first.stop()
    second.stop()
    svc.stop()


@pytest.mark.parametrize('change', [
    {'underlying': 'OTHER'}, {'standard': False}, {'expiry_verified': False},
    {'right': 'put'}, {'strike': 101}, {'multiplier': 10},
])
def test_mismatched_contract_metadata_blocks_execution_and_marks(repo, change):
    row, snap = plan(repo, 'paper')
    svc = service(repo, snap)
    svc.paper_fill(row['id'])
    for key, value in change.items():
        setattr(snap.options[0], key, value)
    with pytest.raises(ValueError, match='metadata'):
        svc.paper_fill(row['id'], intent='close')
    assert svc.position(row['id'])['unrealized_pnl'] is None
    svc.stop()


def test_wrong_underlying_snapshot_is_rejected(repo):
    snap = snapshot()
    snap.underlying = 'OTHER'
    svc = service(repo, snap)
    with pytest.raises(ValueError, match='does not match'):
        svc.snapshot(TradeAdviceRequest(ticker='TEST'))
    svc.stop()


def test_assignment_and_exercise_require_matching_recorded_exposure(repo):
    row, snap = plan(repo)
    svc = service(repo, snap)
    with pytest.raises(ValueError, match='requires recorded'):
        svc.record_fill(row['id'], fill(side='sell', price=0, intent='exercise'))
    svc.record_fill(row['id'], fill())
    with pytest.raises(ValueError, match='requires recorded'):
        svc.record_fill(row['id'], fill(side='buy', price=0, intent='assignment'))
    svc.record_fill(row['id'], fill(side='sell', price=0, intent='exercise'))
    for side, quantity in [('sell', 100), ('buy', 101)]:
        with pytest.raises(ValueError, match='unmatched'):
            svc.record_fill(row['id'], fill(code='TEST', side=side, quantity=quantity,
                                           price=100, intent='exercise'))
    svc.record_fill(row['id'], fill(code='TEST', quantity=100, price=100, intent='exercise'))
    assert svc.position(row['id'])['status'] == 'reconciliation_required'
    svc.stop()


def test_closed_position_status_is_persisted_for_plan_views(repo):
    row, snap = plan(repo, 'paper')
    svc = service(repo, snap)
    svc.paper_fill(row['id'])
    svc.paper_fill(row['id'], intent='close')
    assert repo.plan(row['id'])['status'] == 'closed'
    svc.stop()


def test_fills_with_equal_broker_timestamps_preserve_recorded_order(repo):
    row, snap = plan(repo)
    svc = service(repo, snap)
    timestamp = utcnow() - timedelta(minutes=1)
    opening = {**fill(quantity=2, price=2), 'id': 'z-first', 'filled_at': timestamp}
    closing = {**fill(side='sell', quantity=1, price=4, intent='close'),
               'id': 'a-second', 'filled_at': timestamp}
    svc.record_fill(row['id'], opening)
    result = svc.record_fill(row['id'], closing)
    assert result['realized_pnl'] == pytest.approx(198.7)
    assert result['legs'][0]['average_price'] == 2
    assert [item['id'] for item in repo.fills(row['id'])] == ['z-first', 'a-second']
    assert [item['sequence'] for item in repo.fills(row['id'])] == [1, 2]
    svc.stop()


@pytest.mark.parametrize('code,multiplier', [('OTHER', 1), ('TEST', 100)])
def test_noncanonical_stock_legs_cannot_be_simulated_or_valued(repo, code, multiplier):
    row, snap = plan(repo, 'paper')
    candidate_value = row['candidate']
    candidate_value['legs'].append(OptionLeg(contract_id=code, right='stock',
        side='buy', quantity=100, multiplier=multiplier, entry_price=100).model_dump(mode='json'))
    repo.update_plan(row['id'], {'candidate': candidate_value})
    svc = service(repo, snap)
    with pytest.raises(ValueError, match='Stock leg metadata'):
        svc.paper_fill(row['id'])
    assert not repo.fills(row['id'])
    svc.stop()


def test_reconciliation_requires_complete_share_delivery(repo):
    row, snap = plan(repo)
    svc = service(repo, snap)
    try:
        svc.record_fill(row['id'], fill())
        svc.record_fill(row['id'], fill(side='sell', price=0, intent='exercise'))
        for delivered in (0, 40):
            if delivered:
                svc.record_fill(row['id'], fill(code='TEST', quantity=delivered, price=100, intent='exercise'))
            with pytest.raises(ValueError, match='Record all resulting shares'):
                svc.reconcile(row['id'], 'Acknowledged')
            assert svc.position(row['id'])['status'] == 'reconciliation_required'
            assert not repo.plan(row['id']).get('reconciled_at')
        svc.record_fill(row['id'], fill(code='TEST', quantity=60, price=100, intent='exercise'))
        assert svc.reconcile(row['id'], 'Recorded 100 shares')['status'] == 'open'
        svc.record_fill(row['id'], fill(code='TEST', side='sell', quantity=100, price=101, intent='close'))
        assert svc.reconcile(row['id'], 'Closed resulting shares')['status'] == 'closed'
        assert repo.plan(row['id'])['status'] == 'closed'
    finally:
        svc.stop()


def test_backdated_exercise_reopens_reconciliation(repo):
    row, snap = plan(repo)
    svc = service(repo, snap)
    when = utcnow() - timedelta(days=1)
    def historical(**kw):
        return dict(fill(**kw), filled_at=when)
    try:
        svc.record_fill(row['id'], historical(quantity=2))
        svc.record_fill(row['id'], historical(side='sell', price=0, intent='exercise'))
        svc.record_fill(row['id'], historical(code='TEST', quantity=100, price=100, intent='exercise'))
        assert svc.reconcile(row['id'], 'First exercise reconciled')['status'] == 'open'
        svc.record_fill(row['id'], historical(side='sell', price=0, intent='exercise'))
        svc.record_fill(row['id'], historical(code='TEST', quantity=100, price=100, intent='exercise'))
        assert svc.position(row['id'])['status'] == 'reconciliation_required'
        assert svc.reconcile(row['id'], 'Second exercise reconciled')['status'] == 'open'
    finally:
        svc.stop()


def test_reconciliation_cannot_hide_remaining_expired_options(repo):
    row, snap = plan(repo)
    svc = service(repo, snap)
    try:
        svc.record_fill(row['id'], fill())
        item = row['candidate']
        item['legs'][0]['expiry'] = (utcnow() - timedelta(hours=1)).isoformat()
        repo.update_plan(row['id'], {'candidate': item})
        with pytest.raises(ValueError, match='resolve expired option legs'):
            svc.reconcile(row['id'], 'Expiry is not a closing fill')
    finally:
        svc.stop()


def test_worker_and_paper_execution_request_selected_contracts(repo):
    from src.services.trade_desk.worker import TradeDeskWorker
    row, snap = plan(repo, ledger='paper', mode='replay')
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    try:
        worker.run_once()
        assert svc.provider('replay').required_contracts == ('TEST_CALL',)
        svc.provider('replay').required_contracts = ()
        svc.paper_fill(row['id'])
        assert svc.provider('replay').required_contracts == ('TEST_CALL',)
    finally:
        worker.stop()
        svc.stop()


def test_monitoring_shares_quotes_across_plans_with_the_same_expiry(repo):
    from src.services.trade_desk.worker import TradeDeskWorker
    row, snap = plan(repo, ledger='paper', mode='replay')
    other = candidate(snap)
    other.legs[0].contract_id = 'TEST_CALL_110'
    other.legs[0].strike = 110
    snap.options.append(snap.options[0].model_copy(update={'contract_id': 'TEST_CALL_110', 'strike': 110}))
    second = TradePlan(advice_id=row['advice_id'], candidate=other, ledger='paper', data_mode='replay')
    repo.add_plan(second.model_dump(mode='json'))
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    try:
        svc.paper_fill(row['id'])
        svc.paper_fill(second.id)
        worker.run_once()
        assert svc.provider('replay').required_contracts == ('TEST_CALL', 'TEST_CALL_110')
        assert all(position['valuation_status'] == 'current' for position in svc.positions())
    finally:
        worker.stop()
        svc.stop()


def owned_share_covered_call(repo, shares=100):
    snap = snapshot('replay')
    item = candidate(snap)
    item.strategy = 'covered_call'
    item.legs[0].side = 'sell'
    item.legs.append(OptionLeg(contract_id='TEST', right='stock', side='buy',
        quantity=100, multiplier=1, entry_price=100, existing=True))
    job = repo.create_advice(TradeAdviceRequest(ticker='TEST', data_mode='replay',
                            existing_shares=shares).model_dump(mode='json'))
    repo.update_advice(job['id'], {'status': 'completed', 'candidates': [item.model_dump(mode='json')]})
    svc = service(repo, snap)
    return svc, svc.create_plan(job['id'], item.id)


def test_owned_shares_alone_do_not_open_a_plan_or_block_entry_alerts(repo):
    from src.services.trade_desk.worker import TradeDeskWorker
    svc, row = owned_share_covered_call(repo)
    worker = TradeDeskWorker(svc)
    try:
        position = svc.position(row['id'])
        assert position['status'] == 'watching'
        assert position['legs'][0]['signed_quantity'] == 100
        svc.update_plan(row['id'], {'trigger_price': 99, 'trigger_direction': 'above'})
        worker.run_once()
        assert [e for e in repo.events() if e['event_type'] == 'price_trigger']
    finally:
        worker.stop()
        svc.stop()


def test_paper_close_keeps_explicitly_owned_shares(repo):
    svc, row = owned_share_covered_call(repo)
    try:
        assert svc.paper_fill(row['id'])['status'] == 'open'
        result = svc.paper_fill(row['id'], intent='close')
        assert result['status'] == 'closed'
        assert {leg['contract_id']: leg['signed_quantity'] for leg in result['legs']} == {'TEST': 100}
        assert all(item['contract_id'] != 'TEST' for item in repo.fills(row['id']))
        assert svc.outcomes()['paper_replay']['closed_trades'] == 1
        assert svc.outcomes()['paper']['closed_trades'] == 0
    finally:
        svc.stop()


def test_expired_unfilled_plans_are_not_monitored(repo, monkeypatch):
    import data_provider.us_session
    from src.services.trade_desk.worker import TradeDeskWorker
    monkeypatch.setattr(data_provider.us_session, 'session_window', lambda now=None: ('regular', None, None))
    row, snap = plan(repo, 'paper')
    item = row['candidate']
    item['legs'][0]['expiry'] = (utcnow() - timedelta(hours=1)).isoformat()
    repo.update_plan(row['id'], {'candidate': item})
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    try:
        worker.run_once()
        assert not [e for e in repo.events() if e['event_type'] == 'data_outage']
    finally:
        worker.stop()
        svc.stop()


def test_live_outages_are_not_alerted_outside_the_regular_session(repo, monkeypatch):
    import data_provider.us_session
    from src.services.trade_desk.worker import TradeDeskWorker
    monkeypatch.setattr(data_provider.us_session, 'session_window', lambda now=None: ('closed', None, None))
    row, snap = plan(repo, 'paper')
    snap.options[0].quoted_at -= timedelta(minutes=1)
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    try:
        worker.run_once()
        assert not [e for e in repo.events() if e['event_type'] == 'data_outage']
    finally:
        worker.stop()
        svc.stop()


def test_fill_order_uses_time_not_iso_string_order():
    from src.services.trade_desk.models import TradeFill
    snap = snapshot()
    row = TradePlan(advice_id='a', candidate=candidate(snap), ledger='manual_live').model_dump(mode='json')
    start = utcnow().replace(microsecond=0) - timedelta(minutes=5)
    fills = []
    for sequence, (side, quantity, price, offset, intent) in enumerate([
            ('buy', 2, 2.0, 0, 'open'), ('sell', 1, 3.0, 300000, 'close'),
            ('buy', 1, 5.0, 600000, 'open')], start=1):
        item = TradeFill(plan_id=row['id'], contract_id='TEST_CALL', side=side, quantity=quantity,
                         price=price, filled_at=start + timedelta(microseconds=offset),
                         intent=intent).model_dump(mode='json')
        fills.append(dict(item, sequence=sequence))
    assert fills[0]['filled_at'] > fills[1]['filled_at']  # string order differs from time order
    result = position_from_fills(row, fills)
    assert result['realized_pnl'] == pytest.approx(100)
    assert result['legs'][0]['signed_quantity'] == 2
    assert result['legs'][0]['average_price'] == pytest.approx(3.5)


def test_cancel_does_not_overwrite_a_job_that_already_finished(repo):
    svc = service(repo)
    try:
        job = repo.create_advice(TradeAdviceRequest(ticker='TEST', data_mode='replay').model_dump(mode='json'))
        original_advice = repo.advice
        # The job completes between the cancel's status check and its write.
        def racing_advice(advice_id):
            current = original_advice(advice_id)
            if current and current['status'] == 'queued':
                repo.update_advice(advice_id, {'status': 'completed', 'explanation': 'done'})
            return current
        repo.advice = racing_advice
        result = svc.cancel(job['id'])
        repo.advice = original_advice
        assert result['status'] == 'completed'
        assert repo.advice(job['id'])['explanation'] == 'done'
    finally:
        svc.stop()


def test_quotes_slightly_ahead_of_the_local_clock_are_fresh():
    from src.services.trade_desk.quality import candidate_quotes_fresh, snapshot_fresh
    snap = snapshot()
    ahead = snap.quoted_at + timedelta(milliseconds=800)
    snap.quoted_at = ahead
    snap.options[0].quoted_at = ahead
    now = ahead - timedelta(milliseconds=800)
    assert snapshot_fresh(snap, now=now)
    assert candidate_quotes_fresh(snap, candidate(snap), now=now)
    assert not snapshot_fresh(snap, now=ahead - timedelta(seconds=5))


def test_expired_paper_position_can_be_settled_and_closed(repo):
    row, snap = plan(repo, 'paper', mode='replay')
    svc = service(repo, snap)
    try:
        svc.paper_fill(row['id'])
        item = row['candidate']
        item['legs'][0]['expiry'] = (utcnow() - timedelta(hours=1)).isoformat()
        repo.update_plan(row['id'], {'candidate': item})
        assert svc.position(row['id'])['status'] == 'reconciliation_required'
        with pytest.raises(ValueError, match='underlying price'):
            svc.paper_settle(row['id'], 0)
        result = svc.paper_settle(row['id'], 103)
        assert result['status'] == 'closed'
        assert repo.fills(row['id'])[-1]['price'] == pytest.approx(3)
        assert result['realized_pnl'] == pytest.approx((3 - 2.1) * 100 - 0.65)
        with pytest.raises(ValueError, match='No expired'):
            svc.paper_settle(row['id'], 103)
    finally:
        svc.stop()


def test_leadership_gain_recovers_interrupted_jobs_but_not_its_own(repo):
    from src.services.trade_desk.worker import TradeDeskWorker
    svc = service(repo)
    worker = TradeDeskWorker(svc)
    try:
        orphan = repo.create_advice(TradeAdviceRequest(ticker='TEST').model_dump(mode='json'))
        repo.update_advice(orphan['id'], {'status': 'running'})
        own = repo.create_advice(TradeAdviceRequest(ticker='TEST').model_dump(mode='json'))
        svc._jobs[own['id']] = (threading.Event(), SimpleNamespace(done=lambda: False))
        assert repo.lease('crashed-process')  # a dead leader's lease is still valid
        # Same lease attempt as start(), without its loop thread racing this test.
        worker._leader = repo.lease(worker.owner)
        assert worker._leader is False
        assert repo.advice(orphan['id'])['status'] == 'running'
        repo.release('crashed-process')
        worker.run_once()
        assert repo.advice(orphan['id'])['status'] == 'failed'
        assert repo.advice(own['id'])['status'] == 'queued'
    finally:
        worker.stop()
        svc._jobs.clear()
        svc.stop()


def test_expired_held_positions_raise_reconciliation_not_hourly_outages(repo, monkeypatch):
    import data_provider.us_session
    from src.services.trade_desk.worker import TradeDeskWorker
    monkeypatch.setattr(data_provider.us_session, 'session_window', lambda now=None: ('regular', None, None))
    row, snap = plan(repo, 'paper', mode='replay')
    svc = service(repo, snap)
    worker = TradeDeskWorker(svc)
    try:
        svc.paper_fill(row['id'])
        item = row['candidate']
        item['legs'][0]['expiry'] = (utcnow() - timedelta(hours=1)).isoformat()
        repo.update_plan(row['id'], {'candidate': item})
        worker.run_once()
        types = [e['event_type'] for e in repo.events()]
        assert 'position_reconciliation' in types
        assert 'data_outage' not in types
    finally:
        worker.stop()
        svc.stop()


def test_discovery_skips_non_us_watchlist_codes_and_failing_symbols(repo, monkeypatch):
    from src.services.trade_desk.worker import TradeDeskWorker
    import src.config
    import src.services.us_market_scan
    monkeypatch.setattr(src.config, 'get_config', lambda: SimpleNamespace(stock_list=['600519', 'hk00700', 'BAD', 'TEST']))
    monkeypatch.setattr(src.services.us_market_scan, 'collect_us_market_scan', lambda watchlist: {'gainers': []})
    svc = service(repo)
    worker = TradeDeskWorker(svc)
    try:
        assert worker._discover() == ['BAD', 'TEST']
        calls = []
        def snap_for(request, required_contracts=()):
            calls.append(request.ticker)
            if request.ticker == 'BAD':
                raise RuntimeError('provider failure')
            return snapshot()
        svc.snapshot = snap_for
        submitted = []
        svc.submit = lambda request, source='manual': submitted.append(request.ticker)
        repo.set_preferences({'proactive_enabled': True})
        monkeypatch.setattr('data_provider.us_session.session_window', lambda now=None: ('regular', None, None))
        worker._next_scan = float('inf')
        worker._scan_future = SimpleNamespace(done=lambda: True, result=lambda: ['BAD', 'TEST'])
        worker._proactive()
        assert calls == ['BAD', 'TEST'] and submitted == ['TEST']
    finally:
        worker.stop()
        svc.stop()


@pytest.mark.parametrize('message,allocation,allowed', [
    ('Try it with 5000.', 5000, True), ('What about $5k?', 5000, True),
    ('Use 2.5x leverage', 2, False), ('Compare bearish instead', 700, False),
    ('Go back to the original', 1500, True),
])
def test_follow_up_allocation_must_come_from_the_user(message, allocation, allowed):
    from src.services.trade_desk.tool_worker import effective_request
    request = TradeAdviceRequest(ticker='TEST', allocation=500, message=message)
    if allowed:
        assert effective_request(request, {'allocation': allocation}, original_allocation=1500).allocation == allocation
    else:
        with pytest.raises(ValueError, match='supplied by the user'):
            effective_request(request, {'allocation': allocation}, original_allocation=1500)


def test_material_entry_change_uses_position_size_not_single_leg_ticks():
    from src.services.trade_desk.service import _material_entry_change
    def spread(long_price, short_price):
        return SimpleNamespace(payoff=SimpleNamespace(
            entry_debit=(long_price - short_price) * 100,
            max_loss=(long_price - short_price) * 100 + 1.3))
    # Live AAPL case: the $0.12 wing ticked to $0.11 (8%), the spread moved 3.3%.
    assert not _material_entry_change(spread(2.26, 0.12), spread(2.32, 0.11))
    # A real adverse repricing of the whole spread is still material...
    assert _material_entry_change(spread(2.26, 0.12), spread(2.50, 0.12))
    # ...but a cheaper entry is not (live AAPL: the 2.58/2.81 quote tightened to 2.53/2.60).
    assert not _material_entry_change(spread(2.81, 0.09), spread(2.60, 0.09))
    # Credit spreads are sized by their maximum loss, not the small credit.
    credit = SimpleNamespace(payoff=SimpleNamespace(entry_debit=-23, max_loss=77.3))
    assert not _material_entry_change(credit, SimpleNamespace(payoff=SimpleNamespace(entry_debit=-20, max_loss=80.3)))
    assert _material_entry_change(credit, SimpleNamespace(payoff=SimpleNamespace(entry_debit=-12, max_loss=88.3)))


def test_model_payload_omits_provider_diagnostics_but_keeps_market_warnings():
    from src.services.trade_desk.advisor import _model_warnings
    warnings = ["option_quote_stale:US.SPY260923C700000", "order_book_time_unreported_observed_on_live_subscription",
                "underlying_quote_stale", "expiry_cutoff_nyse_arca_late_close_1615_et", "bars_unavailable:15m:provider_error"]
    assert _model_warnings(warnings) == ["underlying_quote_stale", "expiry_cutoff_nyse_arca_late_close_1615_et",
                                         "bars_unavailable:15m:provider_error"]


_FIXED_EXPIRY = utcnow().replace(microsecond=0) + timedelta(days=7)


def _live_snapshot_at(moment, ask=2.1):
    return QuoteSnapshot(underlying='TEST', spot=100, bid=99.99, ask=100.01, quoted_at=moment,
        provider='test_verified', mode='live', session='regular', source_verified=True,
        options=[OptionQuote(contract_id='TEST_CALL', underlying='TEST', right='call', strike=100,
            expiry=_FIXED_EXPIRY, expiry_verified=True, bid=2.0, ask=ask, quoted_at=moment,
            iv=0.25, bid_size=5, ask_size=5, volume=100, open_interest=1000)])


@pytest.mark.parametrize('later_ask,status', [(2.11, 'completed'), (3.5, 'completed'), (None, 'stale')])
def test_slow_model_call_reprices_instead_of_marking_every_candidate_stale(repo, monkeypatch, later_ask, status):
    from src.services.trade_desk import quality
    start = utcnow()
    clock = [start]
    monkeypatch.setattr(quality, 'utcnow', lambda: clock[0])

    class Provider:
        def snapshot(self, ticker, expiry=None, required_contracts=()):
            if clock[0] == start:
                return _live_snapshot_at(start)
            if later_ask is None:
                raise RuntimeError('quote source briefly unavailable')
            return _live_snapshot_at(clock[0], ask=later_ask)
        def health(self): return {'available': True}
        def close(self): pass

    class SlowWaitingAdvisor:  # recommends waiting, selects nothing, takes 40 seconds
        def explain(self, request, snap, candidates, **kwargs):
            clock[0] = start + timedelta(seconds=40)
            return Narrative(assessment='wait', explanation='Wait for confirmation.'), {}, {}

    from src.services.trade_desk.service import TradeDeskService
    svc = TradeDeskService(repo, lambda mode: Provider(), SlowWaitingAdvisor())
    try:
        job = repo.create_advice(TradeAdviceRequest(ticker='TEST', strategies=['long_call'],
                                                    horizon='swing').model_dump(mode='json'))
        svc._run_advice(job['id'], threading.Event())
        result = repo.advice(job['id'])
        assert result['status'] == status and result['explanation'] == 'Wait for confirmation.'
        if status == 'completed':
            # An unselected candidate is simply re-priced, even after a large move.
            assert result['candidates'][0]['legs'][0]['entry_price'] == pytest.approx(later_ask)
    finally:
        svc.stop()


@pytest.mark.parametrize('phase', ['premarket', 'postmarket', 'closed'])
def test_proactive_runs_only_in_the_regular_session(repo, monkeypatch, phase):
    import data_provider.us_session
    from src.services.trade_desk.worker import TradeDeskWorker
    monkeypatch.setattr(data_provider.us_session, 'session_window', lambda now=None: (phase, None, None))
    repo.set_preferences({'proactive_enabled': True})
    svc = service(repo)
    worker = TradeDeskWorker(svc)
    scans = []
    worker._discover = lambda: scans.append(1) or ['TEST']
    try:
        worker._proactive()
        assert worker._scan_future is None and not scans
        assert not [e for e in repo.events() if e['event_type'] == 'opportunity']
    finally:
        worker.stop()
        svc.stop()


class _Generation:
    def __init__(self, text):
        self.text, self.calls = text, []

    def generate(self, prompt, generation_config, **kwargs):
        from src.llm.generation_backend import GenerationResult
        self.calls.append(kwargs.get('system_prompt') or '')
        return GenerationResult(text=self.text, model='m', provider='p', backend='b', usage={})


def test_model_tiers_route_scans_to_the_routine_model_and_panel_user_requests(repo, monkeypatch,
                                                                              untiered_config):
    from src.services.trade_desk import advisor as advisor_module, analytics
    snap = snapshot('replay')
    item = candidate(snap)
    monkeypatch.setattr(analytics, 'build_candidates', lambda *args: [item])
    untiered_config.targeted_generation_backend = 'codex_cli'
    untiered_config.second_opinion_backends = ['litellm', 'claude_code_cli']
    backends = {
        'litellm': _Generation('{"assessment": "compare", "explanation": "Routine view", "selections": '
                               '[{"candidate_id": "%s", "reason": "Cheapest defined risk"}]}' % item.id),
        'claude_code_cli': _Generation('{"action": "wait", "candidate_id": null, "reason": "No catalyst.", '
                                       '"risk": "Theta."}'),
    }
    monkeypatch.setattr(advisor_module, '_generation_backend',
                        lambda backend_id=None: (backends[backend_id or 'litellm'], backend_id or 'litellm'))

    class Codex:
        calls = 0

        def explain(self, request, snap, candidates, **kwargs):
            Codex.calls += 1
            return Narrative(assessment='compare', explanation='Codex view', selections=[
                Selection(candidate_id=candidates[0].id, reason='test')]), {item.id: item}, {
                    item.id: {'snapshot': snap, 'request': request}}

    svc = service(repo, snap, Codex())
    scan = repo.create_advice(TradeAdviceRequest(ticker='TEST', data_mode='replay').model_dump(mode='json'),
                              source='proactive')
    svc._run_advice(scan['id'], threading.Event())
    scan = repo.advice(scan['id'])
    assert Codex.calls == 0 and scan['explanation'] == 'Routine view' and scan['llm_status'] == 'ready'
    assert 'panel' not in scan and not backends['claude_code_cli'].calls

    # The routine model's panel answer is a verdict, not a Narrative.
    backends['litellm'].text = '{"action": "trade", "candidate_id": "%s", "reason": "Trend.", "risk": "Gap."}' % item.id
    asked = repo.create_advice(TradeAdviceRequest(ticker='TEST', data_mode='replay').model_dump(mode='json'))
    svc._run_advice(asked['id'], threading.Event())
    asked = repo.advice(asked['id'])
    assert Codex.calls == 1 and asked['explanation'] == 'Codex view'
    panel = asked['panel']
    assert [(o['backend'], o['status'], o.get('action')) for o in panel['opinions']] == [
        ('codex_cli', 'ok', 'trade'), ('litellm', 'ok', 'trade'), ('claude_code_cli', 'ok', 'wait')]
    assert panel['opinions'][1]['strategy'] == 'long_call' and panel['agreement'] == 'split'
    assert panel['opinions'][2]['model'] == 'claude-test'
    svc.stop()


def test_panel_rejects_verdicts_on_unknown_candidates():
    from src.services.trade_desk.advisor import _panel_parse, build_panel
    snap = snapshot()
    item = candidate(snap)
    parse = _panel_parse({item.id: item})
    assert parse({'action': 'trade', 'candidate_id': 'invented'}) is None
    assert parse({'action': 'wait', 'candidate_id': item.id})['candidate_id'] is None
    config = SimpleNamespace(litellm_model='openai/g')
    panel = build_panel(None, {item.id: item}, [{'backend': 'litellm', 'status': 'error'}], config)
    assert panel['agreement'] == 'unavailable' and panel['opinions'][0]['status'] == 'error'


def test_stale_quotes_after_the_model_call_keep_the_explanation(repo, monkeypatch):
    from src.services.trade_desk import analytics
    snap = snapshot()
    monkeypatch.setattr(analytics, 'build_candidates', lambda market, request: [candidate(market)])

    class StalingAdvisor:
        def explain(self, request, old, candidates, **kwargs):
            item = candidates[0]
            original = old.model_copy(deep=True)
            snap.stale = True  # quotes age out while the model is working
            return Narrative(assessment='compare', explanation='Conditional comparison',
                selections=[Selection(candidate_id=item.id, reason='test')]), {item.id: item}, {
                    item.id: {'snapshot': original, 'request': request}}

    svc = service(repo, snap, StalingAdvisor())
    job = repo.create_advice(TradeAdviceRequest(ticker='TEST').model_dump(mode='json'))
    svc._run_advice(job['id'], threading.Event())
    result = repo.advice(job['id'])
    assert result['status'] == 'stale' and result['llm_status'] == 'ready'
    assert result['explanation'] == 'Conditional comparison'
    svc.stop()


def test_market_pulse_alerts_are_delivered_with_their_own_label(repo, monkeypatch):
    from src.services.trade_desk import worker as module
    import src.config
    import src.notification
    monkeypatch.setenv('MARKET_PULSE_ENABLED', 'true')
    svc = service(repo)
    worker = module.TradeDeskWorker(svc)
    assert worker._pulse is not None
    repo.set_preferences({'discord_enabled': True})
    repo.event('market_move', {'underlying': 'NVDA', 'message': 'NVDA up 5.2% today (past +5%) at 230.00.'},
               'pulse-move:test')
    monkeypatch.setattr(src.config, 'get_config', lambda: SimpleNamespace(discord_webhook_url='configured'))
    sent = []

    class Delivery:
        def send_to_discord(self, message):
            sent.append(message)
            return True

    monkeypatch.setattr(src.notification, 'NotificationService', Delivery)
    worker._deliver()
    assert sent == ['📈 **NVDA** · Big move\nNVDA up 5.2% today (past +5%) at 230.00.']
    worker.stop()
    svc.stop()

"""Trade Desk API: advice, simulated fills and manually recorded trades only."""
import asyncio
import json
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, Field

from src.services.trade_desk.models import Model, TradeAdviceRequest

router = APIRouter()


def get_service(request: Request):
    service = getattr(request.app.state, "trade_desk_service", None)
    if service is None:
        from src.services.trade_desk.service import TradeDeskService
        service = TradeDeskService()
        request.app.state.trade_desk_service = service
    return service


async def invoke(function, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except RuntimeError as exc:
        from src.services.trade_desk.providers import ProviderError
        if isinstance(exc, ProviderError):
            raise HTTPException(503, detail={"code": exc.code, "message": exc.message}) from exc
        raise


class PlanSelection(Model):
    advice_id: str
    candidate_id: str
    ledger: Literal["paper", "manual_live"] = "paper"
    trigger_price: Optional[float] = Field(default=None, gt=0)
    trigger_direction: Literal["above", "below"] = "above"
    invalidation_price: Optional[float] = Field(default=None, gt=0)
    target_price: Optional[float] = Field(default=None, gt=0)
    exit_at: Optional[AwareDatetime] = None


class PlanUpdate(Model):
    monitoring: Optional[bool] = None
    status: Optional[Literal["invalidated", "archived"]] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    trigger_price: Optional[float] = Field(default=None, gt=0)
    trigger_direction: Optional[Literal["above", "below"]] = None
    invalidation_price: Optional[float] = Field(default=None, gt=0)
    target_price: Optional[float] = Field(default=None, gt=0)
    exit_at: Optional[AwareDatetime] = None


class FillInput(Model):
    contract_id: str
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: float = Field(ge=0)
    fees: float = Field(default=0, ge=0)
    filled_at: AwareDatetime
    intent: Literal["open", "close", "assignment", "exercise"] = "open"
    note: str = Field(default="", max_length=4000)


class PaperFillInput(Model):
    intent: Literal["open", "close"] = "open"
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None


class PaperSettlementInput(Model):
    underlying_price: float = Field(gt=0)


class Preferences(Model):
    proactive_enabled: Optional[bool] = None
    discord_enabled: Optional[bool] = None
    opportunity_daily_limit: Optional[int] = Field(default=None, ge=1, le=50)
    cooldown_minutes: Optional[int] = Field(default=None, ge=1, le=1440)


class Reconciliation(Model):
    notes: str = Field(min_length=1, max_length=4000)


@router.get("/health")
async def health(request: Request):
    return await invoke(get_service(request).health)


@router.get("/catalog")
async def catalog():
    from src.services.trade_desk.analytics import strategy_catalog
    return {"items": strategy_catalog()}


@router.post("/advice", status_code=202)
async def create_advice(body: TradeAdviceRequest, request: Request):
    return await invoke(get_service(request).submit, body)


@router.get("/advice")
async def advice_list(request: Request, limit: int = Query(default=100, ge=1, le=250)):
    return {"items": await invoke(get_service(request).repo.advice_list, limit)}


@router.get("/advice/{advice_id}")
async def advice_detail(advice_id: str, request: Request):
    job = await invoke(get_service(request).repo.advice, advice_id)
    if job is None:
        raise HTTPException(404, detail="Advice not found")
    return job


@router.post("/advice/{advice_id}/cancel")
async def cancel_advice(advice_id: str, request: Request):
    return await invoke(get_service(request).cancel, advice_id)


@router.get("/plans")
async def plans(request: Request):
    return {"items": await invoke(get_service(request).repo.plans)}


@router.post("/plans", status_code=201)
async def create_plan(body: PlanSelection, request: Request):
    return await invoke(get_service(request).create_plan, **body.model_dump(exclude_unset=True))


@router.get("/plans/{plan_id}")
async def plan_detail(plan_id: str, request: Request):
    plan = await invoke(get_service(request).repo.plan, plan_id)
    if plan is None:
        raise HTTPException(404, detail="Plan not found")
    return plan


@router.patch("/plans/{plan_id}")
async def update_plan(plan_id: str, body: PlanUpdate, request: Request):
    return await invoke(get_service(request).update_plan, plan_id, body.model_dump(exclude_unset=True))


@router.post("/plans/{plan_id}/fills", status_code=201)
async def record_fill(plan_id: str, body: FillInput, request: Request):
    return await invoke(get_service(request).record_fill, plan_id, body.model_dump())


@router.post("/plans/{plan_id}/paper-fill", status_code=201)
async def paper_fill(plan_id: str, body: PaperFillInput, request: Request):
    return await invoke(get_service(request).paper_fill, plan_id, **body.model_dump())


@router.post("/plans/{plan_id}/paper-settle", status_code=201)
async def paper_settle(plan_id: str, body: PaperSettlementInput, request: Request):
    return await invoke(get_service(request).paper_settle, plan_id, body.underlying_price)


@router.post("/plans/{plan_id}/reconcile")
async def reconcile(plan_id: str, body: Reconciliation, request: Request):
    return await invoke(get_service(request).reconcile, plan_id, body.notes)


@router.get("/positions")
async def positions(request: Request):
    return {"items": await invoke(get_service(request).positions)}


@router.get("/outcomes")
async def outcomes(request: Request):
    return await invoke(get_service(request).outcomes)


@router.get("/journal")
async def journal(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
    return {"items": await invoke(get_service(request).repo.events, limit=limit, newest=True)}


@router.get("/preferences")
async def preferences(request: Request):
    return await invoke(get_service(request).repo.preferences)


@router.patch("/preferences")
async def update_preferences(body: Preferences, request: Request):
    return await invoke(get_service(request).repo.set_preferences,
                        body.model_dump(exclude_unset=True, exclude_none=True))


class HoldingRuleInput(Model):
    position_key: Optional[str] = Field(default=None, max_length=64)
    ticker: Optional[str] = Field(default=None, max_length=12)
    kind: Literal["price_below", "price_above", "days_to_expiry", "pnl_below", "pnl_above"]
    value: float
    note: str = Field(default="", max_length=200)
    repeat: Literal["once", "daily"] = "once"


class HoldingRuleUpdate(Model):
    status: Optional[Literal["active", "paused"]] = None
    value: Optional[float] = None
    note: Optional[str] = Field(default=None, max_length=200)


class HoldingRuleText(Model):
    text: str = Field(min_length=1, max_length=300)
    position_key: Optional[str] = Field(default=None, max_length=64)


def _holdings(request: Request):
    from src.services.trade_desk import holdings
    if not holdings.enabled():
        raise HTTPException(409, detail="Set TRADE_DESK_BROKER_ACCOUNT to read moomoo holdings")
    return get_service(request).holdings


@router.get("/holdings")
async def holdings_view(request: Request):
    """Read-only broker positions with live marks, plus your alert rules."""
    from src.services.trade_desk import holdings
    if not holdings.enabled():
        return {"enabled": False, "view": None, "rules": [], "error": None}
    service = get_service(request)
    worker = getattr(service, "worker", None)
    error = worker.status().get("holdings_error") if worker is not None else None
    view = await invoke(service.holdings.view)
    return {"enabled": True, "view": view, "rules": service.holdings.rules(), "error": error}


@router.post("/holdings/refresh")
async def holdings_refresh(request: Request):
    store = _holdings(request)
    await invoke(store.sync)
    return {"enabled": True, "view": await invoke(store.view), "rules": store.rules(), "error": None}


@router.post("/holdings/rules", status_code=201)
async def holding_rule_create(body: HoldingRuleInput, request: Request):
    return await invoke(_holdings(request).add_rule, body.model_dump())


@router.post("/holdings/rules/parse")
async def holding_rule_parse(body: HoldingRuleText, request: Request):
    """A rule draft from plain words (pattern first, then the routine model); nothing is saved."""
    from src.services.trade_desk import holdings
    store = _holdings(request)
    draft = holdings.parse_rule_text(body.text)
    source = "pattern"
    if draft is None:
        position = store.position(body.position_key)
        try:
            draft = await asyncio.to_thread(holdings.parse_rule_with_model, body.text,
                                            {k: v for k, v in (position or {}).items() if k != "legs"})
        except Exception:
            draft = None
        source = "model"
    if draft is None:
        raise HTTPException(422, detail="Could not read an alert from that text; pick the type and value instead")
    return {**draft, "position_key": body.position_key, "source": source}


@router.patch("/holdings/rules/{rule_id}")
async def holding_rule_update(rule_id: str, body: HoldingRuleUpdate, request: Request):
    try:
        return await invoke(_holdings(request).update_rule, rule_id, body.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise HTTPException(404, detail="Alert not found") from exc


@router.delete("/holdings/rules/{rule_id}", status_code=204)
async def holding_rule_delete(rule_id: str, request: Request):
    try:
        await invoke(_holdings(request).delete_rule, rule_id)
    except KeyError as exc:
        raise HTTPException(404, detail="Alert not found") from exc


@router.get("/events")
async def events(request: Request, after: Optional[int] = Query(default=None, ge=0)):
    try:
        header = request.headers.get("last-event-id")
        cursor = max(after or 0, int(header or "0"))
    except ValueError as exc:
        raise HTTPException(422, detail="Invalid event cursor") from exc
    service = get_service(request)
    if after is None and header is None:
        # A new client loads current state over REST; replaying the whole
        # history would only trigger redundant refreshes. Reconnects resume
        # from Last-Event-ID, and ?after=0 still requests a full replay.
        latest = await asyncio.to_thread(service.repo.events, limit=1, newest=True)
        cursor = latest[0]["id"] if latest else 0

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            batch = await asyncio.to_thread(service.repo.events, after=cursor, limit=100)
            for event in batch:
                cursor = event["id"]
                yield f"id: {cursor}\ndata: {json.dumps(event, allow_nan=False)}\n\n"
            if not batch:
                yield ": heartbeat\n\n"
                await asyncio.sleep(5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

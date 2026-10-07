"""Where each Trade Desk message goes on Discord, whether it goes at all, and which ones are batched.

Messages fall into four categories. ``TRADE_DESK_DISCORD_WEBHOOKS`` can give each its own
channel (``holdings=<webhook url>,market=<url>,ideas=<url>,digest=<url>``, any subset);
a category without a webhook uses the main Discord setting (``DISCORD_WEBHOOK_URL`` or the
bot). The Trade Desk preferences switch categories on or off (``discord_categories``).

Big-move and news alerts on names you do not hold arrive in bursts at the open; they are
sent together, at most every ``MARKET_BATCH_MINUTES``, as one "Market moves & news" message.
Those on names you hold, and all when holdings are unknown, still go out one by one at once.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

CATEGORIES: Dict[str, frozenset] = {
    "holdings": frozenset({"holding_alert", "portfolio_summary", "price_trigger", "invalidation", "target",
                           "time_exit", "data_outage", "position_reconciliation", "monitor_capacity"}),
    "market": frozenset({"market_move", "market_news"}),
    "ideas": frozenset({"trade_opportunities", "options_ideas", "breakout"}),
    "digest": frozenset({"track_record", "social_digest", "system_status"}),
}
MARKET_BATCH_MINUTES = 15


def category(event_type: str) -> str:
    return next((name for name, types in CATEGORIES.items() if event_type in types), "holdings")


def webhooks() -> Dict[str, str]:
    """Per-category webhook URLs from ``TRADE_DESK_DISCORD_WEBHOOKS``; unknown names are ignored."""
    routes = {}
    for entry in os.getenv("TRADE_DESK_DISCORD_WEBHOOKS", "").split(","):
        name, _, url = entry.strip().partition("=")
        if name.strip() in CATEGORIES and url.strip().startswith("https://"):
            routes[name.strip()] = url.strip()
    return routes


def enabled(preferences: Dict[str, Any], name: str) -> bool:
    return bool((preferences.get("discord_categories") or {}).get(name, True))


def routed(name: str, main: bool) -> bool:
    """Whether the category has somewhere to go: its own webhook or the main Discord setting."""
    return main or name in webhooks()


def fit_batch(events: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """The oldest events whose batch message fits in one Discord message (at least one)."""
    kept: List[Dict[str, Any]] = []
    for event in events:
        if kept and len(batch_message([*kept, event])) > limit:
            break
        kept.append(event)
    return kept


def batchable(event: Dict[str, Any]) -> bool:
    """A move or news alert on a name you do not hold (``held`` is False, not unknown)."""
    return event["event_type"] in CATEGORIES["market"] and event["payload"].get("held") is False


def send(name: str, text: str, embeds: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Posts to the category's own webhook, or through the main Discord setting (with ``embeds``: as cards)."""
    extra = {"embeds": embeds} if embeds else {}
    url = webhooks().get(name)
    if not url:
        from src.notification import NotificationService
        return NotificationService().send_to_discord(text, **extra)
    from src.config import get_config
    from src.notification_sender.discord_sender import DiscordSender
    config = get_config()
    sender = DiscordSender(SimpleNamespace(discord_webhook_url=url, discord_bot_token=None, discord_main_channel_id=None,
                                           discord_max_words=getattr(config, "discord_max_words", 2000),
                                           webhook_verify_ssl=getattr(config, "webhook_verify_ssl", True)))
    return sender.send_to_discord(text, **extra)


def batch_message(events: List[Dict[str, Any]]) -> str:
    """One message for a batch of move and news alerts, oldest first."""
    lines = [f"📊 **Market moves & news** · names you don't hold · last {MARKET_BATCH_MINUTES} min"]
    for event in sorted(events, key=lambda item: item["id"]):
        payload = event["payload"]
        first = str(payload.get("message", "")).split("\n")[0].replace("@", "@​")[:220]
        if event["event_type"] == "market_news":
            icon = "📰"
        else:
            icon = "📈" if (payload.get("change_pct") or 0) >= 0 else "📉"
        lines.append(f"{icon} {first}")
    return "\n".join(lines)


_TONES = {"danger": 0xE5484D, "success": 0x30A46C, "warning": 0xF5A524, "info": 0x3E63DD}
_KIND_TONE = {"loss": "danger", "trend": "danger", "assignment": "danger", "profit": "success",
              "expiry": "warning", "earnings": "warning"}
_REVIEW = "Review in moomoo — nothing is traded automatically."


def _card_from_message(message: str) -> Dict[str, Any]:
    """Alerts stored before cards existed: the first line's lead is the title, the rest the detail."""
    lines = [line for line in message.split("\n") if line and line != _REVIEW]
    title, _, rest = (lines[0] if lines else "").partition(": ")
    return {"title": title, "detail": [rest, *lines[1:]] if rest else lines[1:], "plan": [], "report": "", "alerts": ""}


def _field(line: str) -> Dict[str, Any]:
    name, _, value = line.partition(": ")
    return {"name": name[:256], "value": (value or name)[:1024]}


def position_message(ticker: str, label: str, icon: str, payload: Dict[str, Any], link: str = "") -> tuple:
    """An alert about something you hold: one line for the notification, then a coloured card.

    The card's bar, its "Your position" header and the 💼 mark set it apart from market and idea
    messages; the plan (levels and the usual action), the latest report verdict and your own
    alerts follow as fields. Prices of the stock and percentages only, like every holdings message.
    """
    def safe(text: Any) -> str:
        return str(text or "").replace("@", "@​")
    from .position_plan import FOOTER
    card = payload.get("card") or _card_from_message(str(payload.get("message", "")))
    tone = card.get("tone") or _KIND_TONE.get(str(payload.get("kind")), "info")
    fields = []
    if card.get("plan"):
        fields.append({"name": "What to do", "value": safe("\n".join(card["plan"]))[:1024]})
    fields += [_field(safe(line)) for line in (card.get("report"), card.get("alerts")) if line]
    embed: Dict[str, Any] = {
        "author": {"name": f"💼 YOUR POSITION · {ticker}"},
        "title": safe(f"{icon} {card.get('title') or label}")[:256],
        "description": safe("\n".join(card.get("detail") or []))[:4096],
        "color": _TONES[tone],
        "fields": fields,
        "footer": {"text": FOOTER},
    }
    if link:
        embed["url"] = link
    return f"💼 **{safe(ticker)}** · {safe(label)} · your position", [embed]

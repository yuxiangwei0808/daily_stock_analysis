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
from typing import Any, Dict, List

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


def batchable(event: Dict[str, Any]) -> bool:
    """A move or news alert on a name you do not hold (``held`` is False, not unknown)."""
    return event["event_type"] in CATEGORIES["market"] and event["payload"].get("held") is False


def send(name: str, text: str) -> bool:
    """Posts to the category's own webhook, or through the main Discord setting."""
    url = webhooks().get(name)
    if not url:
        from src.notification import NotificationService
        return NotificationService().send_to_discord(text)
    from src.config import get_config
    from src.notification_sender.discord_sender import DiscordSender
    config = get_config()
    sender = DiscordSender(SimpleNamespace(discord_webhook_url=url, discord_bot_token=None, discord_main_channel_id=None,
                                           discord_max_words=getattr(config, "discord_max_words", 2000),
                                           webhook_verify_ssl=getattr(config, "webhook_verify_ssl", True)))
    return sender.send_to_discord(text)


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

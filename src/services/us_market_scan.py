"""Scoped US market breadth, sector ETF proxies and outside-watchlist movers."""
import logging
from datetime import datetime, timezone
from src.services.screening.snapshot_us import fetch_us_snapshot, fetch_us_universe
from data_provider.us_session import session_window
from src.services.screening.source_guard import call_with_timeout, parse_source_timeout_seconds

logger = logging.getLogger(__name__)
SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLI": "Industrials",
    "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication Services",
}


def collect_us_market_scan(watchlist=(), *, now=None):
    """Use a single timestamp and snapshot for comparable breadth and movers."""
    import os
    now = now or datetime.now(timezone.utc)
    result = {"available": False, "session": session_window(now)[0], "warnings": []}
    if result["session"] == "closed":
        result["warnings"] = ["No active US session (holiday, overnight or calendar unavailable)."]
        return result
    source = os.getenv("SCREENING_US_UNIVERSE", "auto")
    try:
        universe = [code.replace("-", ".") for code in fetch_us_universe(source)]
        snapshot = call_with_timeout(
            fetch_us_snapshot, list(dict.fromkeys([*universe, *SECTOR_ETFS])), now=now,
            timeout_sec=parse_source_timeout_seconds("SCREENING_SNAPSHOT_CALL_TIMEOUT_SEC", default=60),
            label="US market session scan",
        )
        snapshot = snapshot.copy()
        day_basis = (result["session"] == "postmarket" and "day_change_pct" in snapshot
                     and len(snapshot) and snapshot["day_change_pct"].notna().mean() >= 0.5)
        if day_basis:
            # After the close, breadth, sectors and movers describe the regular session;
            # the after-hours move stays beside it and drives the unverified-mover list.
            snapshot["after_hours_pct"] = snapshot["change_pct"]
            snapshot["after_hours_amount"] = snapshot["amount"]
            snapshot["change_pct"] = snapshot["day_change_pct"]
            snapshot["amount"] = snapshot["day_amount"].fillna(snapshot["amount"])
        result["change_basis"] = "regular_session" if day_basis else "session"
        equities = snapshot[snapshot.code.isin(universe) & snapshot.change_pct.notna()]
        sectors = snapshot[snapshot.code.isin(SECTOR_ETFS) & snapshot.change_pct.notna()].sort_values(
            "change_pct", ascending=False)
        tracked = {str(code).strip().upper().replace("-", ".") for code in watchlist}
        outside = equities[(~equities.code.isin(tracked)) & (equities.price >= 5)]
        candidates = outside[outside.amount >= 1_000_000]
        unverified_movers = []
        after_hours_movers = []
        if result["session"] in {"premarket", "postmarket"}:
            # Extended-session moves: after the close these use the after-hours columns.
            move, amount = ("after_hours_pct", "after_hours_amount") if day_basis else ("change_pct", "amount")
            extended = outside.assign(change_pct=outside[move], amount=outside[amount])
            unverified = extended[extended.amount.fillna(0).le(0) & extended.change_pct.abs().ge(2)]
            unverified = unverified.assign(move_size=unverified.change_pct.abs()).sort_values(
                "move_size", ascending=False, kind="stable",
            ).head(10)
            unverified_movers = [
                {**row, "liquidity_status": "unverified"}
                for row in unverified[["code", "price", "change_pct", "provider_timestamp"]].to_dict("records")
            ]
            if day_basis:
                liquid = extended[(extended.amount >= 1_000_000) & extended.change_pct.abs().ge(2)]
                after_hours_movers = liquid.assign(move_size=liquid.change_pct.abs()).sort_values(
                    "move_size", ascending=False, kind="stable",
                ).head(8)[["code", "price", "change_pct", "amount", "provider_timestamp"]].to_dict("records")
        fields = ["code", "price", "change_pct", "volume", "amount", "provider_timestamp"]
        sector_rows = [{"name": f"{SECTOR_ETFS[row.code]} ({row.code} ETF)", "code": row.code,
                        "change_pct": row.change_pct, "provider_timestamp": row.provider_timestamp}
                       for row in sectors.itertuples()]
        result.update({
            "available": not equities.empty, "as_of": snapshot.attrs.get("as_of"),
            "universe_source": source, "requested_count": len(universe),
            "available_count": len(equities), "excluded_count": len(universe) - len(equities),
            "coverage_note": snapshot.attrs.get("coverage_note"),
            "advancers": int((equities.change_pct > 0).sum()),
            "decliners": int((equities.change_pct < 0).sum()),
            "unchanged": int((equities.change_pct == 0).sum()),
            "gainers": candidates[candidates.change_pct >= 2].sort_values(
                "change_pct", ascending=False, kind="stable",
            ).head(5)[fields].to_dict("records"),
            "losers": candidates[candidates.change_pct <= -2].sort_values(
                "change_pct", ascending=True, kind="stable",
            ).head(5)[fields].to_dict("records"),
            "unverified_movers": unverified_movers, "after_hours_movers": after_hours_movers,
            "sectors": sector_rows, "sector_available_count": len(sector_rows),
            "warnings": snapshot.attrs.get("source_errors", []),
        })
    except Exception as exc:
        logger.warning("US market scan unavailable: %s", exc)
        result["warnings"] = [f"US scan unavailable: {type(exc).__name__}. No current breadth or movers inferred."]
    return result


def render_us_market_scan(scan, language="en"):
    if not scan:
        return ""
    title = "US session scan" if language == "en" else "美股时段扫描"
    lines = [f"### {title}"]
    lines.extend(str(warning) for warning in scan.get("warnings", []))
    if not scan.get("available"):
        return "\n\n".join(lines)
    day_basis = scan.get("change_basis") == "regular_session"
    lines += [
        f"As of: {scan.get('as_of')} | Session: {scan['session']} | Source: Yahoo Finance 5-minute bars"
        + (" | Moves below are today's regular session (close vs previous close), not after-hours"
           if day_basis else ""),
        f"Coverage: {scan['available_count']}/{scan['requested_count']} configured stocks; "
        f"{scan['excluded_count']} excluded. Sector ETF proxies: {scan['sector_available_count']}/11.",
        "Universe breadth only (not all US listings): "
        f"{scan['advancers']} up / {scan['decliners']} down / {scan['unchanged']} unchanged.",
        "Premarket/regular changes use the prior regular close; after-hours changes use today's regular close. "
        "Prices may be delayed. Liquidity-qualified candidates require price >= $5 and estimated session dollar volume >= $1M. "
        "Extended-hours price movers with unverified liquidity are listed separately. "
        "Movements identify research candidates; catalysts and continuation are unverified.",
    ]
    for label, key in (("Gainers outside your watchlist", "gainers"), ("Decliners outside your watchlist", "losers")):
        lines.append(f"#### {label}")
        rows = scan.get(key, [])
        if not rows:
            lines.append("No qualifying fresh candidates.")
            continue
        lines += [f"| Symbol | Price USD | {'Day move' if day_basis else 'Session move'} | "
                  f"Estimated {'day' if day_basis else 'session'} USD volume | Quote time |",
                  "|---|---:|---:|---:|---|"]
        lines += [f"| {r['code']} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['amount']:,.0f} | {r['provider_timestamp']} |"
                  for r in rows]
    if scan.get("after_hours_movers"):
        lines += ["#### After-hours movers outside your watchlist",
                  "| Symbol | Price USD | After-hours move | Estimated after-hours USD volume | Quote time |",
                  "|---|---:|---:|---:|---|"]
        lines += [f"| {r['code']} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['amount']:,.0f} | {r['provider_timestamp']} |"
                  for r in scan["after_hours_movers"]]
    if scan.get("unverified_movers"):
        label = ("Price movers outside your watchlist — liquidity unverified" if language == "en"
                 else "自选股外价格异动 — 流动性未验证")
        explanation = (
            "Session volume is unavailable or reported as zero. These price moves do not meet the "
            "liquidity-qualified filter; tradable liquidity, catalysts and continuation are unverified."
            if language == "en" else
            "时段成交量缺失或报为零。这些价格异动未通过流动性筛选；可交易流动性、催化因素及走势持续性均未验证。"
        )
        lines += [f"#### {label}", explanation,
                  "| Symbol | Price USD | Session move | Quote time |", "|---|---:|---:|---|"]
        lines += [f"| {r['code']} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['provider_timestamp']} |"
                  for r in scan["unverified_movers"]]
    lines += ["#### Sector ETF performance (proxies)",
              f"| ETF / sector | {'Day move' if day_basis else 'Session move'} | Quote time |", "|---|---:|---|"]
    lines += [f"| {r['name']} | {r['change_pct']:+.2f}% | {r['provider_timestamp']} |" for r in scan.get("sectors", [])]
    return "\n\n".join(lines[:6]) + "\n\n" + "\n".join(lines[6:])

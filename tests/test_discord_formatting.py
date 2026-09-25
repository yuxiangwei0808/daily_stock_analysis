"""Discord-only rendering: tables and code blocks across message splits."""
from src.notification_sender.discord_sender import _balance_code_fences, _discord_markdown


def test_markdown_tables_become_narrow_aligned_code_blocks():
    table = (
        "Intro\n"
        "| Index | Last | Turnover |\n"
        "|-------|------|----------|\n"
        "| **S&P 500 (regular-session daily bar: 2026-09-24)** | 7704.13 | N/A |\n"
        "| Nasdaq | 26939.37 | N/A |\n"
        "---\n"
        "After"
    )
    out = _discord_markdown(table)
    assert out.split("\n") == ["Intro", "```", "Index    Last", "S&P 500  7704.13", "Nasdaq   26939.37", "```", "",
                               "After"]


def test_code_blocks_split_across_messages_stay_closed():
    chunks = _balance_code_fences(["head\n```\nAAPL 1", "NVDA 2\n```\ntail", "plain"])
    assert chunks == ["head\n```\nAAPL 1\n```", "```\nNVDA 2\n```\ntail", "plain"]

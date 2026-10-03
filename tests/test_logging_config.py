# -*- coding: utf-8 -*-
"""Regression tests for application logging configuration."""

import logging

import pytest

from src.logging_config import LITELLM_LOGGERS, setup_logging


@pytest.fixture(autouse=True)
def restore_logging_state():
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    original_handlers = list(root_logger.handlers)
    original_litellm_levels = {
        logger_name: logging.getLogger(logger_name).level
        for logger_name in LITELLM_LOGGERS
    }

    yield

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        if handler not in original_handlers:
            handler.close()
    for handler in original_handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(original_root_level)

    for logger_name, level in original_litellm_levels.items():
        logging.getLogger(logger_name).setLevel(level)


def _read_debug_log(log_dir) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    debug_log = next(log_dir.glob("stock_analysis_debug_*.log"))
    return debug_log.read_text(encoding="utf-8")


def test_log_format_includes_logger_name(tmp_path, monkeypatch):
    monkeypatch.delenv("LITELLM_LOG_LEVEL", raising=False)

    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    logging.getLogger("src.sample").info("logger context smoke")

    debug_log_text = _read_debug_log(tmp_path)
    assert " | src.sample | " in debug_log_text
    assert "logger context smoke" in debug_log_text


@pytest.mark.parametrize("env_value", [None, "", "  "])
def test_litellm_debug_is_quiet_by_default_and_empty_env(tmp_path, monkeypatch, env_value):
    if env_value is None:
        monkeypatch.delenv("LITELLM_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LITELLM_LOG_LEVEL", env_value)

    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    for logger_name in LITELLM_LOGGERS:
        logging.getLogger(logger_name).debug("%s token debug should be filtered", logger_name)
    logging.getLogger("LiteLLM").warning("litellm warning should remain")
    logging.getLogger("src.sample").debug("project debug should remain")

    debug_log_text = _read_debug_log(tmp_path)

    for logger_name in LITELLM_LOGGERS:
        assert f"{logger_name} token debug should be filtered" not in debug_log_text
    assert "litellm warning should remain" in debug_log_text
    assert "project debug should remain" in debug_log_text


def test_litellm_log_level_debug_restores_litellm_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOG_LEVEL", "DEBUG")

    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    for logger_name in LITELLM_LOGGERS:
        logging.getLogger(logger_name).debug("%s debug should remain", logger_name)

    debug_log_text = _read_debug_log(tmp_path)

    for logger_name in LITELLM_LOGGERS:
        assert f"{logger_name} debug should remain" in debug_log_text


def test_invalid_litellm_log_level_falls_back_to_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOG_LEVEL", "verbose")

    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    logging.getLogger("LiteLLM").debug("invalid level debug should be filtered")
    logging.getLogger("LiteLLM").warning("invalid level warning should remain")

    debug_log_text = _read_debug_log(tmp_path)

    assert "invalid level debug should be filtered" not in debug_log_text
    assert "invalid level warning should remain" in debug_log_text
    assert "LITELLM_LOG_LEVEL" in debug_log_text
    assert "已回退为 WARNING" in debug_log_text


def test_yfinance_logger_is_not_debug_enabled_so_downloads_stay_threaded(tmp_path):
    # yfinance falls back to sequential multi-symbol downloads when its logger
    # is DEBUG-enabled; with the root DEBUG handler this made the US scan time out.
    yf_logger = logging.getLogger("yfinance")
    original = yf_logger.level
    try:
        setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)
        assert logging.getLogger().isEnabledFor(logging.DEBUG)
        assert not yf_logger.isEnabledFor(logging.DEBUG)
    finally:
        yf_logger.setLevel(original)


def test_log_files_older_than_the_retention_are_removed(tmp_path, monkeypatch):
    import os
    import time

    from src.logging_config import prune_old_logs
    now = time.time()
    names = {"stock_analysis_20260801.log": 40, "stock_analysis_debug_20260801.log.2": 40,
             "stock_analysis_20260930.log": 3, "notes.txt": 40}
    for name, age_days in names.items():
        path = tmp_path / name
        path.write_text("x")
        os.utime(path, (now - age_days * 86400, now - age_days * 86400))
    monkeypatch.delenv("LOG_RETENTION_DAYS", raising=False)
    assert prune_old_logs(tmp_path, now=now) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt", "stock_analysis_20260930.log"]
    monkeypatch.setenv("LOG_RETENTION_DAYS", "0")  # keeps everything
    (tmp_path / "old.log").write_text("x")
    os.utime(tmp_path / "old.log", (now - 400 * 86400, now - 400 * 86400))
    assert prune_old_logs(tmp_path, now=now) == 0

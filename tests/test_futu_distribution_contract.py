# -*- coding: utf-8 -*-
"""Distribution contracts for the globally bundled Futu SDK."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _workflow(relative_path: str) -> dict:
    return yaml.load(_read(relative_path), Loader=yaml.BaseLoader)


def _job_run_text(job: dict) -> str:
    return "\n".join(
        str(step.get("run", ""))
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )


def test_futu_sdk_is_pinned_and_verified_in_ci_and_the_daily_workflow() -> None:
    requirements = _read("requirements.txt")
    ci = _workflow(".github/workflows/ci.yml")
    daily = _workflow(".github/workflows/00-daily-analysis.yml")

    assert requirements.count("futu-api==10.8.6808") == 1
    assert "import futu" in _job_run_text(ci["jobs"]["backend-tests"])
    assert "import futu" in _job_run_text(daily["jobs"]["analyze"])

"""
One-off helper: prints every Activity Type in this Didar account, so you
can pick the right Id for DIDAR_ACTIVITY_TYPE_CALL_ID / _SMS_ID / _TASK_ID
in .env (see src/didar/activity_client.py's module docstring for why
these can't be hardcoded - they're per-account).

Reads DIDAR_BASE_URL / DIDAR_API_KEY straight from the project's own
.env via src.config, so there's nothing to paste in by hand.

Run from the project root (with the venv activated):
    python -m scripts.list_activity_types
"""
from __future__ import annotations

import httpx

from src.config import settings


def main() -> None:
    cfg = settings.didar
    if not cfg.base_url or not cfg.api_key:
        print("DIDAR_BASE_URL / DIDAR_API_KEY are not set in .env - fill those in first.")
        return

    resp = httpx.post(
        f"{cfg.base_url}/activity/GetActivityType",
        params={"apikey": cfg.api_key},
        timeout=30.0,
    )
    resp.raise_for_status()
    types = resp.json().get("Response", [])

    if not types:
        print("No activity types returned - check the API key/permissions.")
        return

    print(f"{'Title':<30} {'Icon':<20} Id")
    print("-" * 90)
    for t in types:
        print(f"{t.get('Title', ''):<30} {t.get('Icon', ''):<20} {t.get('Id', '')}")


if __name__ == "__main__":
    main()

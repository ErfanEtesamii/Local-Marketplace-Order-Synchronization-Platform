"""
One-off helper: prints every Deal Label in this Didar account (via the
documented GET /Label/GetDealLabels - same endpoint
DidarDealClient._label_id_by_title_map() uses internally), so you can
confirm the EXACT Title text to put in .env's
DIDAR_DEAL_LABEL_TITLE_TAPSISHOP / _DIGIKALA / _BASALAM / _SNAPPSHOP /
_FARAZHONAR (see src/config.py's deal_label_title_by_source docstring).

Why this matters: DidarDealClient._label_id_for_source() only ever
matches by exact Title (after Persian-character normalization - see
src/didar/category_mapping.py's _normalize_fa) against whatever this
endpoint actually returns for THIS account. A Title configured in .env
that doesn't match exactly means that source's Deals get created with
NO label at all - silently, logged only as a WARNING (never an error,
by design - see _label_id_for_source's docstring), so it's easy to miss
until someone notices a Deal in the Didar UI with no colored label on
it.

Only entries with Type == "Deal" are printed, matching the filter
_label_id_by_title_map() itself applies (Didar's Label list can include
other label Types too, e.g. for Contacts).

Reads DIDAR_BASE_URL / DIDAR_API_KEY straight from the project's own
.env via src.config, so there's nothing to paste in by hand.

Run from the project root (with the venv activated):
    python -m scripts.list_deal_labels
"""
from __future__ import annotations

import httpx

from src.config import settings


def main() -> None:
    cfg = settings.didar
    if not cfg.base_url or not cfg.api_key:
        print("DIDAR_BASE_URL / DIDAR_API_KEY are not set in .env - fill those in first.")
        return

    resp = httpx.get(
        f"{cfg.base_url}{cfg.get_deal_labels_path}",
        params={"apikey": cfg.api_key},
        timeout=30.0,
    )
    resp.raise_for_status()
    labels = resp.json().get("Response", [])

    deal_labels = [l for l in labels if isinstance(l, dict) and l.get("Type") == "Deal"]

    if not deal_labels:
        print(
            "No Deal Labels returned (Type == 'Deal') - either this account has none "
            "configured yet, or check the API key/permissions."
        )
        return

    print(f"{'Title':<30} {'Code':<10} Id")
    print("-" * 90)
    for label in deal_labels:
        print(f"{label.get('Title', ''):<30} {label.get('Code', ''):<10} {label.get('Id', '')}")

    print()
    print("Compare each Title above, character-for-character, against .env's")
    print("DIDAR_DEAL_LABEL_TITLE_* values (src/config.py's deal_label_title_by_source)")
    print("- these must match exactly (after Persian ي/ك->ی/ک + ZWNJ normalization) or")
    print("that source's Deals will be created with no label. In particular, check")
    print("DIDAR_DEAL_LABEL_TITLE_FARAZHONAR (default 'سایت فرامرزی') against whatever")
    print("this account's real Faraz Honar label Title is - recent logs show every")
    print("Faraz Honar deal failing this match and being created without a label.")


if __name__ == "__main__":
    main()

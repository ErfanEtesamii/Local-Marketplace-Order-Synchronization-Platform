"""
Diagnostic: dump every raw Deal in a date range, with the Titles of
ALL Labels attached to each one - not just the single label this
project's own per-source/per-label reports each query for.

WHY THIS EXISTS: the /report custom-range picker (see src/telegram.py's
_send_custom_range_report) computes each label's count/total via
POST /deal/search_v2 with Criteria.LabelIds=[<that one label's Id>].
Per Didar's own docs, that filter returns every Deal that HAS this
label - it does NOT require the label to be the deal's ONLY label
(the docs are explicit: "Deal با LabelIds به یک یا چند Deal Label وصل
می‌شود" - a Deal can carry more than one). So if a single Deal happens
to carry two labels (e.g. both اسنپ and دیجی‌کالا), it gets counted -
with its full price - under BOTH labels' report lines, which is
exactly the kind of "extra count that matches another label's total"
mismatch this script is meant to help you find and confirm by eye,
one raw Deal at a time, rather than guessing from aggregate totals.

This calls the SAME /deal/search_v2 endpoint the reports use, but with
NO LabelIds filter at all (just the date range) and Limit high enough
to return every row, so the response's "List" includes each Deal's own
LabelIds array in full - see DidarDealClient.get_status_breakdown_for_label()
for the aggregate-only version of this same call.

Run from the project root (with the venv activated):
    python -m scripts.dump_range_deals 1405-06-14 1405-06-14

Arguments are Jalali dates (start and end, inclusive), same format the
/report picker's date buttons produce internally. Defaults to today if
omitted.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jdatetime

import src.config  # noqa: F401  (loads .env via load_dotenv on import)
from src.config import settings
from src.didar.deal_client import DidarDealClient, _iso
from src.telegram import IRAN_TZ, _iran_midnight_utc


def _parse_jalali(s: str) -> "jdatetime.date":
    y, m, d = (int(part) for part in s.split("-"))
    return jdatetime.date(y, m, d)


def main() -> None:
    cfg = settings.didar
    if not cfg.base_url or not cfg.api_key:
        print("DIDAR_BASE_URL / DIDAR_API_KEY are not set in .env - fill those in first.")
        return

    args = sys.argv[1:]
    today = jdatetime.date.fromgregorian(date=__import__("datetime").date.today())
    start_date = _parse_jalali(args[0]) if len(args) >= 1 else today
    end_date = _parse_jalali(args[1]) if len(args) >= 2 else start_date

    since = _iran_midnight_utc(start_date)
    until = _iran_midnight_utc(end_date + timedelta(days=1))

    client = DidarDealClient(config=cfg)

    # Title -> Id for every Deal Label, so we can print human-readable
    # names next to each Deal's LabelIds instead of raw GUIDs.
    labels = client.list_deal_labels()
    title_by_id = {label_id: title for title, label_id in labels}

    payload = client._post(  # noqa: SLF001 - deliberate, this is a one-off diagnostic script
        "/deal/search_v2",
        json={
            "Criteria": {
                "SearchFromTime": _iso(since),
                "SearchToTime": _iso(until),
            },
            "From": 0,
            "Limit": 200,
        },
    )
    response = payload.get("Response", {})
    rows = response.get("List", [])

    if not rows:
        print("No deals found in this range.")
        return

    print(f"از {start_date} تا {end_date}  ({len(rows)} معامله)")
    print("-" * 100)
    multi_label_count = 0
    for row in rows:
        label_ids = row.get("LabelIds") or []
        label_titles = [title_by_id.get(lid, f"?{lid}") for lid in label_ids]
        flag = ""
        if len(label_titles) > 1:
            flag = "  <-- بیش از یک لیبل!"
            multi_label_count += 1
        print(
            f"Id={row.get('Id')}  Price={row.get('Price')}  "
            f"Status={row.get('Status')}  Labels={label_titles}{flag}"
        )

    print("-" * 100)
    print(f"{multi_label_count} معامله از {len(rows)} تا بیش از یک لیبل دارند.")
    if multi_label_count:
        print(
            "این معامله‌ها توی گزارش هر برچسبی که دارن، دوباره (با کل مبلغشون) "
            "شمرده می‌شن - همون چیزی که باعث بالاتر بودن جمعِ دیجی‌کالا/فرازهنر "
            "از مقدار واقعی می‌شه."
        )


if __name__ == "__main__":
    main()

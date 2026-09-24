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

import csv
import sys
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jdatetime

import src.config  # noqa: F401  (loads .env via load_dotenv on import)
from src.config import settings
from src.didar.deal_client import (
    DidarDealClient,
    _iso,
    _parse_didar_datetime,
    _search_to_time,
)
from src.telegram import IRAN_TZ, _iran_midnight_utc


_PAGE = 50


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

    # Same call shape as DidarDealClient.get_channel_report(): pipeline
    # scoped, SearchToTime widened to max(until, now) (see
    # _search_to_time()), every page fetched. Rows are then split into
    # "in report" (RegisterTime in [since, until)) and "returned but
    # outside" so the raw response can be diffed against a Didar Excel
    # export by Deal Id.
    criteria = {
        "SearchFromTime": _iso(since),
        "SearchToTime": _iso(_search_to_time(until)),
        "Sort": 0,
    }
    if cfg.pipeline_id:
        criteria["PipelineId"] = cfg.pipeline_id

    rows: list[dict] = []
    offset = 0
    while True:
        payload = client._post(  # noqa: SLF001 - deliberate, this is a one-off diagnostic script
            "/deal/search_v2",
            json={"Criteria": criteria, "From": offset, "Limit": _PAGE},
        )
        page = (payload.get("Response") or {}).get("List") or []
        rows.extend(r for r in page if isinstance(r, dict) and r.get("Id"))
        if len(page) < _PAGE:
            break
        offset += _PAGE

    if not rows:
        print("No deals found in this range.")
        return

    def _fmt(d) -> str:
        return f"{d.year}-{d.month:02d}-{d.day:02d}"

    label_a, label_b = _fmt(start_date), _fmt(end_date)
    out_path = Path(f"deals_{label_a}_{label_b}.csv")
    by_status: dict[str, list] = {}
    edge_rows: list[str] = []
    edge = timedelta(hours=12)
    in_report = 0
    total = Decimal("0")
    multi_label_count = 0
    seen: set[str] = set()
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Id", "InReport", "RegisterTime", "ChangeToWonTime",
                         "Status", "Price", "Labels"])
        for row in rows:
            deal_id = str(row["Id"])
            if deal_id in seen:
                continue
            seen.add(deal_id)
            registered = _parse_didar_datetime(row.get("RegisterTime"))
            inside = registered is not None and since <= registered < until
            label_titles = [title_by_id.get(lid, f"?{lid}") for lid in (row.get("LabelIds") or [])]
            if len(label_titles) > 1:
                multi_label_count += 1
            if inside:
                in_report += 1
                st = str(row.get("Status"))
                by_status.setdefault(st, [0, Decimal("0")])
                by_status[st][0] += 1
                try:
                    by_status[st][1] += Decimal(str(row.get("Price")))
                except (InvalidOperation, TypeError, ValueError):
                    pass
            if registered is not None and (
                abs(registered - since) <= edge or abs(registered - until) <= edge
            ):
                edge_rows.append(f"  Id={deal_id} RegisterTime={row.get('RegisterTime')} "
                                 f"Price={row.get('Price')} InReport={int(inside)}")
            if inside:
                try:
                    total += Decimal(str(row.get("Price")))
                except (InvalidOperation, TypeError, ValueError):
                    pass
            writer.writerow([deal_id, int(inside), row.get("RegisterTime"),
                             row.get("ChangeToWonTime"), row.get("Status"),
                             row.get("Price"), " | ".join(label_titles)])

    print(f"از {label_a} تا {label_b}")
    for st, (c, t) in by_status.items():
        print(f"  Status={st}: {c} معامله / {t:,}")
    if edge_rows:
        print("معامله‌های نزدیک مرز بازه (±۱۲ ساعت):")
        print("\n".join(edge_rows))
    print(f"ردیف‌های برگشتی از API : {len(seen)}")
    print(f"داخل گزارش (RegisterTime در بازه): {in_report}")
    print(f"جمع مبلغ داخل گزارش : {total:,}")
    print(f"معامله با بیش از یک لیبل : {multi_label_count}")
    print(f"فایل خروجی برای مقایسه با اکسل دیدار (بر اساس Id): {out_path.resolve()}")


if __name__ == "__main__":
    main()
"""
Post-sale checklist timing rules.

Client rule (2026-08 feedback, follow-up to the original checklist
feedback in activity_client.py's module docstring): every checklist
item's due date is now derived from a single anchor - the order's
ship time (زمان ارسال محصول) - except پیامک 1, which is anchored to
when the order was registered instead. Kept as its own module (rather
than inline in activity_client.py) so the pure date-math is trivially
unit-testable without any HTTP mocking.

Confirmed rules, as given by the client:
  - پیامک ۱:                 order registration time + 5 hours
  - پیامک ۲ / تماس جدید:      ship time - 1 hour
  - ارسال محصول:              ship time itself (the ship activity's own
                               due date - i.e. "this needs to happen by")
  - پیامک ۳ / تماس رضایت:      ship time + 5 days

ship_time itself must come from the marketplace's API (see
NormalizedOrder.ship_time) - this module only does the arithmetic
from that anchor, it never invents a ship time.
"""
from __future__ import annotations

from datetime import datetime, timedelta

_ONE_HOUR = timedelta(hours=1)
_FIVE_HOURS = timedelta(hours=5)
_FIVE_DAYS = timedelta(days=5)


def compute_checklist_due_dates(
    order_registered_at: datetime, ship_time: datetime,
) -> dict[str, datetime]:
    """
    Returns {checklist title: due date}, one entry per title in
    POST_SALE_CHECKLIST (src/didar/activity_client.py) - keeping the two
    in sync is enforced by test_scheduling.py rather than duplicating
    the title list here.
    """
    due_dates = {
        "تماس جدید": ship_time - _ONE_HOUR,
        "پیامک 2": ship_time - _ONE_HOUR,
        "پیامک 1": order_registered_at + _FIVE_HOURS,
        "ارسال محصول": ship_time,
        "پیامک 3": ship_time + _FIVE_DAYS,
        "تماس رضایت": ship_time + _FIVE_DAYS,
    }
    # Imported lazily (rather than at module level) to avoid a circular
    # import: activity_client.py imports compute_checklist_due_dates
    # from this module, and POST_SALE_CHECKLIST is defined there.
    from src.didar.activity_client import POST_SALE_CHECKLIST

    checklist_titles = {title for title, _ in POST_SALE_CHECKLIST}
    assert checklist_titles == due_dates.keys(), (
        "scheduling.py's title set has drifted from "
        "activity_client.POST_SALE_CHECKLIST - update both together"
    )
    return due_dates

from datetime import datetime, timedelta, timezone

from src.didar.scheduling import compute_checklist_due_dates

_REGISTERED = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
_SHIP = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)


def test_computes_every_checklist_titles_due_date():
    due_dates = compute_checklist_due_dates(order_registered_at=_REGISTERED, ship_time=_SHIP)

    assert due_dates["پیامک 1"] == _REGISTERED + timedelta(hours=5)
    assert due_dates["پیامک 2"] == _SHIP - timedelta(hours=1)
    assert due_dates["تماس جدید"] == _SHIP - timedelta(hours=1)
    assert due_dates["ارسال محصول"] == _SHIP
    assert due_dates["پیامک 3"] == _SHIP + timedelta(days=5)
    assert due_dates["تماس رضایت"] == _SHIP + timedelta(days=5)


def test_covers_exactly_the_post_sale_checklist_titles():
    from src.didar.activity_client import POST_SALE_CHECKLIST

    due_dates = compute_checklist_due_dates(order_registered_at=_REGISTERED, ship_time=_SHIP)
    assert set(due_dates.keys()) == {title for title, _ in POST_SALE_CHECKLIST}

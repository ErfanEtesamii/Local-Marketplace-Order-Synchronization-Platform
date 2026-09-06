"""
Full manual/interactive smoke test for src/telegram.py.

Unlike scripts/test_telegram_hashtags.py (which only re-sends three
hardcoded literal strings), this script exercises the REAL code paths
end-to-end against your real, configured bot:

    - is_configured() / getMe() connectivity check
    - notify_new_order()  (both the "no fixed shipping fee" case and the
      Digikala "fixed shipping fee" case - see src/shipping_fees.py)
    - notify_new_deal()
    - the daily / weekly / monthly / yearly aggregate reports
      (_send_daily_report / _send_weekly_report / _send_monthly_report /
      _send_yearly_report - the same private methods check_and_send_reports()
      calls on a real rollover)
    - the notification retry queue (record -> queued on failure -> cleared
      on a later successful retry), i.e. notification_failures table +
      retry_pending_notifications()
    - the interactive /report date-range picker (poll_updates()), live,
      so you can actually click the inline-keyboard buttons in Telegram
      and watch this script react in real time

Everything here writes to a THROWAWAY sqlite file (a fresh tempfile,
printed at startup and left on disk for inspection - delete it whenever),
never to your real ./data/sync.db. That matters because this script pokes
at report_progress markers and notification_failures rows directly, and
those must never collide with your production bot's real state.

Like scripts/test_telegram_hashtags.py, this deliberately calls private
methods/attributes (_send_daily_report, _deliver, _chat_ids, ...) that
production code never touches directly - acceptable here because this
file's only job is poking at those exact code paths from the outside,
same precedent as the hashtag script.

Run from the project root:

    python scripts/test_telegram_bot_full.py

Requires TELEGRAM_BOT_TOKEN and at least one TELEGRAM_CHAT_ID(_N) to
already be set in .env.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jdatetime

import src.config  # noqa: F401  (loads .env via load_dotenv on import)
from src.db.repository import Repository
from src.didar.deal_poller import NewDealInfo
from src.marketplaces.base import NormalizedOrder, OrderItem
from src.telegram import IRAN_TZ, TelegramNotifier, _iranian_weekday

# Platforms whose reports get exercised - matches src/telegram.py's
# _PLATFORM_DISPLAY keys (minus snappshop, disabled by default).
_PLATFORMS = ["digikala", "basalam", "tapsishop", "farazhonar"]


# ----------------------------------------------------------------------
# Sample data builders
# ----------------------------------------------------------------------
def _sample_order(source: str, source_order_id: str) -> NormalizedOrder:
    items = [
        OrderItem(
            sku="TEST-SKU-1", title="محصول تست یک",
            quantity=2, unit_price=Decimal("500000"), final_price=Decimal("1000000"),
        ),
        OrderItem(
            sku="TEST-SKU-2", title="محصول تست دو",
            quantity=1, unit_price=Decimal("250000"), final_price=Decimal("250000"),
        ),
    ]
    return NormalizedOrder(
        source=source,
        source_order_id=source_order_id,
        order_number=source_order_id,
        created_at=datetime.now(timezone.utc),
        total_price=Decimal("1300000"),
        status="test",
        items=items,
        customer_full_name="مشتری تستی",
        shipping_cost=Decimal("50000"),
    )


def _sample_deal() -> NewDealInfo:
    return NewDealInfo(
        deal_id="TEST-DEAL-0001",
        code=99999,
        title="معامله تست",
        customer_name="مشتری تستی",
        price=Decimal("1300000"),
        owner_name="ادمین تست",
        stage_name="در حال بررسی",
        register_time=datetime.now(timezone.utc),
    )


def _seed_report_data(repository: Repository) -> None:
    """A handful of synced_orders rows, all timestamped 'now' (see
    mark_synced - synced_at isn't overridable), which lands inside
    today/this-week/this-month/this-year no matter when this script is
    run - so the same seed data can back all four report tests."""
    seeds = [
        ("digikala", "SEED-DK-1", 1_000_000, 239_000 * 10, 3_390_000),
        ("digikala", "SEED-DK-2", 2_000_000, 239_000 * 10, 4_390_000),
        ("basalam", "SEED-BL-1", 1_500_000, 0, 1_500_000),
        ("tapsishop", "SEED-TS-1", 800_000, 50_000, 850_000),
        ("farazhonar", "SEED-FH-1", 1_200_000, 2_250_000, 3_450_000),
    ]
    for platform, order_id, products, shipping, total in seeds:
        repository.mark_synced(
            platform, order_id, f"deal-{order_id}",
            products_amount=products, shipping_amount=shipping, total_amount=total,
        )
    print(f"  seeded {len(seeds)} synced_orders rows for report tests")


# ----------------------------------------------------------------------
# Individual tests
# ----------------------------------------------------------------------
def test_connectivity(notifier: TelegramNotifier) -> bool:
    print("\n[1] بررسی اتصال (is_configured / getMe)...")
    ok = notifier.is_configured()
    if ok:
        print(f"  OK - {len(notifier._chat_ids)} گیرنده تنظیم شده: {notifier._chat_ids!r}")
    else:
        print("  FAILED - TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID(_N) را در .env بررسی کنید.")
    return ok


def test_new_order_plain(notifier: TelegramNotifier, repository: Repository) -> None:
    print("\n[2] ارسال «سفارش جدید» - باسلام (بدون هزینه ارسال ثابت)...")
    order = _sample_order("basalam", "TEST-ORDER-BASALAM")
    notifier.notify_new_order(order, "test-deal-id", repository)
    print("  ارسال شد - تلگرام را چک کنید.")


def test_new_order_fixed_shipping(notifier: TelegramNotifier, repository: Repository) -> None:
    print("\n[3] ارسال «سفارش جدید» - دیجی‌کالا (با هزینه ارسال ثابت)...")
    order = _sample_order("digikala", "TEST-ORDER-DIGIKALA")
    notifier.notify_new_order(order, "test-deal-id", repository)
    print("  ارسال شد - هزینه ارسال باید ۲,۳۹۰,۰۰۰ ریال نمایش داده شود.")


def test_new_deal(notifier: TelegramNotifier, repository: Repository) -> None:
    print("\n[4] ارسال «معامله جدید»...")
    notifier.notify_new_deal(_sample_deal(), repository)
    print("  ارسال شد - تلگرام را چک کنید.")


def test_daily_report(notifier: TelegramNotifier, repository: Repository) -> None:
    print("\n[5] ارسال گزارش روزانه...")
    now_local = datetime.now(timezone.utc).astimezone(IRAN_TZ)
    today = jdatetime.date.fromgregorian(date=now_local.date())
    notifier._send_daily_report(repository, _PLATFORMS, today)
    print("  ارسال شد.")


def test_weekly_report(notifier: TelegramNotifier, repository: Repository) -> None:
    print("\n[6] ارسال گزارش هفتگی...")
    now_local = datetime.now(timezone.utc).astimezone(IRAN_TZ)
    today = jdatetime.date.fromgregorian(date=now_local.date())
    week_start = today - timedelta(days=_iranian_weekday(today))
    notifier._send_weekly_report(repository, _PLATFORMS, week_start)
    print("  ارسال شد.")


def test_monthly_report(notifier: TelegramNotifier, repository: Repository) -> None:
    print("\n[7] ارسال گزارش ماهانه...")
    now_local = datetime.now(timezone.utc).astimezone(IRAN_TZ)
    today = jdatetime.date.fromgregorian(date=now_local.date())
    month_first = jdatetime.date(today.year, today.month, 1)
    notifier._send_monthly_report(repository, _PLATFORMS, month_first)
    print("  ارسال شد.")


def test_yearly_report(notifier: TelegramNotifier, repository: Repository) -> None:
    print("\n[8] ارسال گزارش سالانه...")
    now_local = datetime.now(timezone.utc).astimezone(IRAN_TZ)
    today = jdatetime.date.fromgregorian(date=now_local.date())
    year_first = jdatetime.date(today.year, 1, 1)
    notifier._send_yearly_report(repository, _PLATFORMS, year_first)
    print("  ارسال شد.")


def test_retry_queue(notifier: TelegramNotifier, repository: Repository) -> None:
    """Forces a real send failure (an invalid chat id), confirms it lands
    in notification_failures, restores the real chat id(s), then confirms
    retry_pending_notifications() successfully resends and clears it."""
    print("\n[9] تست صف ری‌تری (ارسال ناموفق و سپس تلاش مجدد موفق)...")
    ref_id = "test:retry-queue"
    real_chat_ids = notifier._chat_ids
    try:
        notifier._chat_ids = [1]  # guaranteed-invalid chat id -> guaranteed failure
        notifier._deliver(ref_id, "پیام تست صف ری‌تری", repository, "retry-queue test message")
    finally:
        notifier._chat_ids = real_chat_ids

    pending = repository.get_pending_notification_failures()
    queued = any(f.ref_id == ref_id for f in pending)
    print(f"  queued after forced failure: {queued}")
    if not queued:
        print("  FAILED - پیام در صف ری‌تری ثبت نشد.")
        return

    notifier.retry_pending_notifications(repository)
    pending_after = repository.get_pending_notification_failures()
    cleared = not any(f.ref_id == ref_id for f in pending_after)
    print(f"  cleared after successful retry: {cleared}")
    if cleared:
        print("  OK - پیام تست باید همین الان در تلگرام رسیده باشد.")
    else:
        print("  FAILED - پیام بعد از تلاش مجدد هم در صف باقی ماند.")


def test_report_picker_live(
    notifier: TelegramNotifier, repository: Repository, duration_seconds: int = 120
) -> None:
    print(
        f"\n[10] اجرای زنده دستور /report برای {duration_seconds} ثانیه.\n"
        "     همین الان در تلگرام دستور /report را بفرستید و روی دکمه‌ها بزنید -\n"
        "     پیشرفت هر مرحله همین‌جا هم چاپ می‌شود. برای توقف زودتر Ctrl+C بزنید."
    )
    end = time.time() + duration_seconds
    try:
        while time.time() < end:
            notifier.poll_updates(repository, _PLATFORMS)
            time.sleep(2)
    except KeyboardInterrupt:
        print("  متوقف شد توسط کاربر.")
    print("  پایان تست زنده /report.")


# ----------------------------------------------------------------------
# Menu
# ----------------------------------------------------------------------
_MENU = """
=== تست کامل بات تلگرام ===
 1) بررسی اتصال و تنظیمات
 2) سفارش جدید (باسلام - بدون هزینه ارسال ثابت)
 3) سفارش جدید (دیجی‌کالا - با هزینه ارسال ثابت)
 4) معامله جدید
 5) گزارش روزانه
 6) گزارش هفتگی
 7) گزارش ماهانه
 8) گزارش سالانه
 9) صف ری‌تری (ارسال ناموفق -> تلاش مجدد موفق)
10) اجرای زنده /report (کلیک روی دکمه‌ها در تلگرام)
11) اجرای همه موارد ۱ تا ۹ به ترتیب
 0) خروج
انتخاب شما: """


def main() -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="telegram_bot_test_", suffix=".db")
    os.close(tmp_fd)
    Path(tmp_path).unlink()  # Repository() creates its own file; just needed a free name
    print(f"از یک دیتابیس موقت و جدا استفاده می‌شود: {tmp_path}")
    print("(این فایل به دیتابیس واقعی پروژه دست نمی‌زند - هر وقت خواستید حذفش کنید.)")

    repository = Repository(db_path=tmp_path)
    notifier = TelegramNotifier()

    if not test_connectivity(notifier):
        sys.exit(1)

    _seed_report_data(repository)

    actions = {
        "2": lambda: test_new_order_plain(notifier, repository),
        "3": lambda: test_new_order_fixed_shipping(notifier, repository),
        "4": lambda: test_new_deal(notifier, repository),
        "5": lambda: test_daily_report(notifier, repository),
        "6": lambda: test_weekly_report(notifier, repository),
        "7": lambda: test_monthly_report(notifier, repository),
        "8": lambda: test_yearly_report(notifier, repository),
        "9": lambda: test_retry_queue(notifier, repository),
        "10": lambda: test_report_picker_live(notifier, repository),
    }

    while True:
        choice = input(_MENU).strip()
        if choice == "0":
            break
        elif choice == "11":
            for key in ("2", "3", "4", "5", "6", "7", "8", "9"):
                actions[key]()
                time.sleep(1)  # be gentle with Telegram's rate limits
        elif choice in actions:
            actions[choice]()
        else:
            print("گزینه نامعتبر است.")

    notifier.close()
    print("پایان تست.")


if __name__ == "__main__":
    main()

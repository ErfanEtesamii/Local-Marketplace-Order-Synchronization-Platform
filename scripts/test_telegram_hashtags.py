"""
Manual smoke test for the telegram.py hashtag fix.

Loads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID(s) from .env (via src.config)
and sends the three affected message shapes (new order, new deal, report)
straight to real Telegram, exactly as _format_*_message() would produce
them, so you can visually confirm the #hashtags actually render as
tappable hashtags (blue/linked) instead of plain text.

Run from the project root:

    python scripts/test_telegram_hashtags.py

Requires TELEGRAM_BOT_TOKEN and at least one TELEGRAM_CHAT_ID(_N) to
already be set in .env - the script just calls TelegramNotifier()._send()
directly, bypassing Repository/order/deal objects entirely.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.config  # noqa: F401  (loads .env via load_dotenv on import)
from src.telegram import TelegramNotifier


def main() -> None:
    notifier = TelegramNotifier()

    if not notifier.is_configured():
        print(
            "TelegramNotifier is not configured - set TELEGRAM_BOT_TOKEN "
            "and TELEGRAM_CHAT_ID (or TELEGRAM_CHAT_ID_1..10) in .env "
            "before running this script."
        )
        sys.exit(1)

    messages = {
        "new order": (
            "🟢 سفارش جدید ثبت شد\n"
            "🛍 پلتفرم:\n"
            "🟢 باسلام\n"
            "👤 مشتری:\n"
            "علی رضایی\n"
            "📦 محصولات:\n"
            "1️⃣ محصول تست\n"
            "   └─ 50,000 ریال × 2\n"
            "🚚 هزینه ارسال:\n"
            "30,000 ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💰 مبلغ محصولات:\n"
            "100,000 ریال\n"
            "🚚 ارسال:\n"
            "30,000 ریال\n"
            "💳 مبلغ کل:\n"
            "130,000 ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🕐 ۱۴۰۵/۰۶/۱۵ — ۱۸:۴۲\n"
            "🟢 ثبت موفق در دیدار\n"
            "#باسلام"
        ),
        "new deal": (
            "🔔 معامله جدید در دیدار\n"
            "📌 عنوان:\n"
            "معامله تست\n"
            "👤 مشتری:\n"
            "علی رضایی\n"
            "💰 مبلغ:\n"
            "130,000 ریال\n"
            "🧑\u200d💼 مسئول:\n"
            "نامشخص\n"
            "🚦 مرحله:\n"
            "نامشخص\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🕐 ۱۴۰۵/۰۶/۱۵ — ۱۸:۴۲\n"
            "شناسه معامله: #DEAL123\n"
            "#معامله_تست"
        ),
        "report": (
            "📊 گزارش پایان روز\n"
            "📅 شنبه ۱۴۰۵/۰۶/۰۷\n"
            "🛒 تعداد سفارش‌های موفق\n"
            "└─ 4 سفارش\n"
            "💰 مبلغ فروش محصولات\n"
            "└─ 1,000,000 ریال\n"
            "🚚 مجموع هزینه ارسال\n"
            "└─ 50,000 ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💳 مجموع فروش\n"
            "└─ 1,050,000 ریال\n"
            "📈 میانگین هر سفارش\n"
            "└─ 262,500 ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 همه سفارش‌ها با موفقیت\n"
            "در دیدار ثبت شده‌اند.\n"
            "#گزارش"
        ),
    }

    for label, text in messages.items():
        print(f"Sending '{label}' test message...")
        try:
            notifier._send(text)
            print(f"  OK - check Telegram and confirm the hashtag is tappable/highlighted.")
        except Exception as exc:  # noqa: BLE001 - this is a manual diagnostic script
            print(f"  FAILED: {exc!r}")

    print("\nDone. Check your Telegram chat(s) for 3 messages.")


if __name__ == "__main__":
    main()

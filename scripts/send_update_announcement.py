"""
One-off: tell every configured Telegram recipient that the bot was
updated and that they must press "start" once to get the new menu
buttons (Telegram keeps the OLD bottom keyboard until the bot sends a
new one, and the bot only sends it on /start).

The message carries an inline "🚀 شروع" button (callback "rpt:start").
Pressing it is handled by the RUNNING service's poll_updates() exactly
like typing /start - so deploy the new code and restart the service
BEFORE running this script, otherwise the button does nothing.

Run from the project root (needs TELEGRAM_BOT_TOKEN and at least one
TELEGRAM_CHAT_ID(_N) in .env):

    python scripts/send_update_announcement.py

It lists the recipients and asks for confirmation before sending.
Sending only needs sendMessage - it never calls getUpdates, so it can't
steal updates from the running service.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.config  # noqa: F401  (loads .env via load_dotenv on import)
from src.telegram import (
    _REPORT_CALLBACK_PREFIX,
    TelegramNotifier,
    _inline_keyboard,
)

MESSAGE = (
    "🔔 بروزرسانی جدید بات\n\n"
    "دکمه‌های جدید دریافت گزارش ۲۴ ساعت، ۷ روز و ۳۰ روز گذشته اضافه شد.\n\n"
    "لطفاً برای فعال شدن منوی جدید، بات را مجدداً استارت بزنید؛ "
    "کافیست روی دکمه‌ی «🚀 شروع» زیر همین پیام بزنید 👇"
)


def main() -> None:
    notifier = TelegramNotifier()
    if not notifier.is_configured():
        print("Telegram is not configured (check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID*).")
        return

    chat_ids = list(notifier._chat_ids)
    print("Will send the update announcement to:")
    for chat_id in chat_ids:
        print(f"  - {notifier._display_name(chat_id)} ({chat_id})")
    if input("Send? [y/N] ").strip().lower() != "y":
        print("Cancelled - nothing sent.")
        return

    keyboard = _inline_keyboard([[("🚀 شروع", f"{_REPORT_CALLBACK_PREFIX}start")]])
    for chat_id in chat_ids:
        message_id = notifier._send_message_with_keyboard(chat_id, MESSAGE, keyboard)
        print(f"  {'sent' if message_id else 'FAILED'}: {chat_id}")
    notifier.close()


if __name__ == "__main__":
    main()

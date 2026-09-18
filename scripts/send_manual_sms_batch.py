"""
One-off script: send a batch SMS to a fixed list of staff, each with
their own name substituted into the message text.

Usage:
    python -m scripts.send_manual_sms_batch --platform "دیجی کالا"
    python -m scripts.send_manual_sms_batch --test   # "this is a test" text to everyone

Reads MODIR_PAYAMAK_TOKEN / MODIR_PAYAMAK_FROM_NUMBER from the project's
.env (same credentials src/modir_payamak.py uses for the automated
express-order alert) and reuses this project's own HTTP retry policy
(src/http_utils.default_retry) so a transient 5xx/network error is
retried the same way every other external call in this codebase is.

This is intentionally separate from src/modir_payamak.py:
ModirPayamakNotifier is wired to the automated express-order flow (5
fixed warehouse recipients, one fixed message shape, dedup against a
specific order). This script is a manual, ad-hoc batch send to the
same fixed set of people and does not touch that module's
dedup/retry-queue state.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.http_utils import default_retry, raise_for_status_with_body  # noqa: E402

_IPPANEL_API_BASE = "https://edge.ippanel.com/v1/api"

# (name as it appears in the message, phone number)
RECIPIENTS = [
    ("آقای ترابی", "09136938054"),
    ("آقای خراطی", "09133097726"),
    ("روابط عمومی", "09199192270"),
    ("فرازهنر شعبه تهران", "09912662014"),
    ("حسابداری", "09136499817"),
    ("آقای اعتصامی", "09921190031"),
]


def build_message(name: str, platform: str) -> str:
    return f"{name} عزیز سفارش اکسپرس از پلتفرم {platform} ثبت شده لطفا پیگیر باشید با تشکر"


def build_test_message(name: str) -> str:
    return f"سلام {name} این یک پیام تست است از طرف اعتصامی با تشکر"


@default_retry()
def _send_one(client: httpx.Client, phone: str, text: str) -> dict:
    resp = client.post(
        "/send",
        json={
            "sending_type": "webservice",
            "from_number": os.environ["MODIR_PAYAMAK_FROM_NUMBER"],
            "message": text,
            "params": {"recipients": [phone]},
        },
    )
    raise_for_status_with_body(resp)
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", help='e.g. "دیجی کالا" (ignored with --test)')
    parser.add_argument(
        "--test", action="store_true",
        help="send the 'this is a test' message to everyone instead of the express-order one",
    )
    parser.add_argument("--dry-run", action="store_true", help="print messages, don't send")
    args = parser.parse_args()
    if not args.test and not args.platform:
        sys.exit("--platform is required unless --test is passed")

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.getenv("MODIR_PAYAMAK_TOKEN", "").strip()
    from_number = os.getenv("MODIR_PAYAMAK_FROM_NUMBER", "").strip()
    if not args.dry_run and (not token or not from_number):
        sys.exit("MODIR_PAYAMAK_TOKEN / MODIR_PAYAMAK_FROM_NUMBER not set in .env")

    client = None
    if not args.dry_run:
        client = httpx.Client(
            base_url=_IPPANEL_API_BASE,
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=15.0,
        )

    for name, phone in RECIPIENTS:
        text = build_test_message(name) if args.test else build_message(name, args.platform)
        if args.dry_run:
            print(f"[dry-run] {phone}: {text}")
            continue
        try:
            _send_one(client, phone, text)
            print(f"[ok] sent to {phone} ({name})")
        except Exception as exc:
            print(f"[FAILED] {phone} ({name}): {exc}")

    if client is not None:
        client.close()


if __name__ == "__main__":
    main()
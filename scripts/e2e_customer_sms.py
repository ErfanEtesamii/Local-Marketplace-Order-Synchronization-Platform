"""
End-to-end rehearsal of the first customer SMS (پیامک ۱) with a REAL send
to ONE number you choose (normally your own).

It uses the real CustomerSmsNotifier with the settings from your .env
(SMS_GATEWAY_*, SMS_GATEWAY_CLOUD_*, SMS_GATEWAY_SIM_NUMBER, ...) but a
throw-away temporary database, so it never touches data/sync.db and never
contacts any marketplace or Didar. It checks, in order:

  1. an eligible order whose time has come   -> exactly ONE SMS is sent
  2. the same order enqueued/processed again -> NO second SMS (dedup)
  3. an order the status check says is cancelled -> NO SMS (skipped)
  4. an order not yet due (5h not elapsed)   -> NO SMS yet
  5. an order with no usable phone number    -> nothing queued

Expected result: your phone receives exactly ONE message (the real text).

Usage (from the project root, with the venv active):
    python -m scripts.e2e_customer_sms --to 09921190031
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402  (loads .env)
from src.customer_sms import CustomerSmsNotifier, normalize_iran_mobile  # noqa: E402
from src.db.repository import Repository  # noqa: E402
from src.marketplaces.base import NormalizedOrder  # noqa: E402


def _order(oid: str, mobile: str | None, created: datetime, status: str = "processing"):
    return NormalizedOrder(
        source="farazhonar", source_order_id=oid, order_number=oid, created_at=created,
        total_price=Decimal("1"), status=status, customer_mobile=mobile,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="your mobile, e.g. 09921190031")
    args = ap.parse_args()

    phone = normalize_iran_mobile(args.to)
    if not phone:
        print(f"[ERROR] {args.to!r} is not a valid Iranian mobile number")
        return 2
    if settings.dry_run:
        print("[ERROR] DRY_RUN=true in .env - nothing would be sent. Set it to false for this test.")
        return 2

    # Force the feature on, drop quiet hours and the dry-run switch for this
    # process only (the .env file itself is not changed).
    os.environ["CUSTOMER_SMS_ENABLED"] = "true"
    os.environ["CUSTOMER_SMS_DRY_RUN"] = "false"
    os.environ["CUSTOMER_SMS_QUIET_START"] = os.environ["CUSTOMER_SMS_QUIET_END"] = "0"
    os.environ.pop("CUSTOMER_SMS_PLATFORMS", None)

    notifier = CustomerSmsNotifier()
    if not notifier.is_enabled():
        print("[ERROR] no gateway route configured in .env (SMS_GATEWAY_* / SMS_GATEWAY_CLOUD_*)")
        return 2

    sent_texts: list[tuple[str, str]] = []
    real_send = notifier._send

    def counting_send(p, text, now=None):
        result = real_send(p, text, now)
        sent_texts.append((p, text))
        return result

    notifier._send = counting_send  # count only what really went out

    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Repository(str(Path(tmp) / "e2e.db"))
        results: list[tuple[str, bool]] = []

        def check(name: str, ok: bool) -> None:
            results.append((name, ok))
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

        def live(platform: str, order_id: str):
            return None  # status check stub: order still valid

        due_created = now - timedelta(hours=5, minutes=1)

        print(f"Real SMS will be sent to {phone} (exactly one expected)...\n")

        # 1) eligible + due -> one SMS
        notifier.enqueue(_order("E2E-OK", args.to, due_created), repo, now=now)
        notifier.process_due(repo, now=now, status_check=live)
        check("eligible due order -> 1 SMS sent", len(sent_texts) == 1)

        # 2) dedup: enqueue + process again -> still 1
        notifier.enqueue(_order("E2E-OK", args.to, due_created), repo, now=now)
        notifier.process_due(repo, now=now + timedelta(minutes=2), status_check=live)
        check("same order again -> no second SMS", len(sent_texts) == 1)

        # 3) cancelled at send time -> skipped, nothing sent
        notifier.enqueue(_order("E2E-CANCEL", args.to, due_created), repo, now=now)
        notifier.process_due(
            repo, now=now + timedelta(minutes=3),
            status_check=lambda p, o: "status cancelled is cancelled/failed",
        )
        check("order cancelled before sending -> skipped", len(sent_texts) == 1)

        # 4) not due yet -> nothing
        notifier.enqueue(_order("E2E-LATER", args.to, now - timedelta(hours=1)), repo, now=now)
        notifier.process_due(repo, now=now + timedelta(minutes=4), status_check=live)
        check("order registered 1h ago (not due) -> not sent yet", len(sent_texts) == 1)

        # 5) no phone -> not queued
        notifier.enqueue(_order("E2E-NOPHONE", "021-1234", due_created), repo, now=now)
        notifier.process_due(repo, now=now + timedelta(minutes=5), status_check=live)
        check("order without mobile number -> nothing sent", len(sent_texts) == 1)

    print()
    if sent_texts:
        print(f"Message that was sent to {sent_texts[0][0]}:\n---\n{sent_texts[0][1]}\n---")
    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"\nFAILED checks: {failed}")
        return 1
    print("\nAll checks passed. Now confirm on the phone: exactly ONE message arrived, "
          "from Irancell, with the full text (Messages app > Sent / Delivered).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

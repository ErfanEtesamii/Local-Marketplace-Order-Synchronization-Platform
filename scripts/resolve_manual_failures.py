"""
One-off helper: orders that are stuck in sync_failures but were already
entered into Didar BY HAND should stop being retried and stop being
re-fetched. This moves every matching sync_failures row onto the
permanent skip-list (ignored_orders - see repository.py schema docstring,
point 7) and deletes the failure row, which also silences the recurring
"health_check: <platform> has N order(s) stuck in retry" warning.

Motivating case (2026-10): 7 SnappShop orders failed with Didar's
"duplicate product code" (SnappShop sent no item title - fixed since in
snappshop.py/snappshop2.py), hit the 5-attempt cap, and were then entered
into Didar manually.

Run from the project root (venv activated):
    python -m scripts.resolve_manual_failures --dry-run
    python -m scripts.resolve_manual_failures                    # snappshop + snappshop2
    python -m scripts.resolve_manual_failures --platform snappshop
    python -m scripts.resolve_manual_failures --ids 978906224 1530025871

Without --ids it takes EVERY current sync_failures row for the chosen
platform(s) - check the --dry-run list first, so a genuinely new failure
that you have NOT entered by hand is not swept up with the old ones.
Safe to run while the service is running (SQLite), and safe to re-run.
"""
from __future__ import annotations

import argparse

from src.db.repository import Repository

_DEFAULT_PLATFORMS = ("snappshop", "snappshop2")
_REASON = "manually_entered_in_didar"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", action="append", help="Repeatable; default: snappshop and snappshop2")
    parser.add_argument("--ids", nargs="*", default=None, help="Only these source_order_ids")
    parser.add_argument("--dry-run", action="store_true", help="List what would change, change nothing")
    parser.add_argument("--db-path", default=None, help="Override DB_PATH (default: src.config.settings)")
    args = parser.parse_args()

    platforms = tuple(args.platform) if args.platform else _DEFAULT_PLATFORMS
    wanted = set(args.ids) if args.ids else None

    repo = Repository(db_path=args.db_path)
    with repo._connect() as conn:
        rows = conn.execute(
            "SELECT platform, source_order_id, attempt_count, last_attempt_at FROM sync_failures"
        ).fetchall()

    todo = [
        r for r in rows
        if r[0] in platforms and (wanted is None or r[1] in wanted)
    ]
    if not todo:
        print(f"No matching sync_failures rows for platform(s) {', '.join(platforms)} - nothing to do.")
        return

    for platform, order_id, attempts, last in todo:
        print(f"  {platform}  order {order_id}  attempts={attempts}  last_attempt={last}")

    if args.dry_run:
        print(f"[dry-run] {len(todo)} row(s) would be ignored + cleared.")
        return

    for platform in {r[0] for r in todo}:
        ids = [r[1] for r in todo if r[0] == platform]
        repo.add_ignored_ids(platform, ids, reason=_REASON)
        for order_id in ids:
            repo.clear_failure(platform, order_id)
    print(f"Done: {len(todo)} order(s) added to ignored_orders and removed from the retry queue.")


if __name__ == "__main__":
    main()

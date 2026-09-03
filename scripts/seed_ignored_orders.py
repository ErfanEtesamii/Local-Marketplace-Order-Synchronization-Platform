"""
One-off helper: bulk-seeds the ignored_orders permanent skip-list (see
repository.py's schema docstring, point 7) from a TSV file of order ids
that are already known to be out-of-window - so they stop being
re-fetched/re-evaluated/re-logged on every poll cycle starting NOW,
instead of waiting for sync_engine._sync_source() to naturally
re-encounter and drop each one at least once more first.

Motivating case (2026-09): ~1,056 pre-watermark Digikala orders (some
dating back to 2024-11) were being window-dropped and re-logged on
every single poll cycle - see sync_engine.py's _sync_source() layer-0
skip-list comment for the permanent fix. This script is only needed to
retroactively seed ids collected BEFORE that fix existed (e.g. from an
exported log analysis); ids dropped by the running code from now on
are added to the table automatically and never need this script.

Expected input: a TSV file with a header row, first column =
source_order_id (any extra columns, e.g. a created_at, are ignored).
This matches the digikala_dropped_orders.tsv export produced from the
project's own logs.

Run from the project root (with the venv activated):
    python -m scripts.seed_ignored_orders --platform digikala path/to/digikala_dropped_orders.tsv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from src.db.repository import Repository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tsv_path", type=Path, help="TSV file; first column = source_order_id")
    parser.add_argument("--platform", required=True, help="e.g. digikala")
    parser.add_argument(
        "--reason",
        default="outside_window_backfill",
        help="Stored in ignored_orders.reason (default: outside_window_backfill)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override DB_PATH (defaults to whatever src.config.settings uses)",
    )
    args = parser.parse_args()

    with args.tsv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        order_ids = [row[0].strip() for row in reader if row and row[0].strip()]

    if not order_ids:
        print(f"No order ids found in {args.tsv_path} (header read as {header!r}) - nothing to do.")
        return

    repo = Repository(db_path=args.db_path)
    already_ignored = repo.get_ignored_ids(args.platform)
    new_ids = [oid for oid in order_ids if oid not in already_ignored]

    repo.add_ignored_ids(args.platform, order_ids, reason=args.reason)

    print(
        f"seed_ignored_orders: {len(order_ids)} id(s) read from {args.tsv_path} for "
        f"platform={args.platform!r} - {len(new_ids)} newly added, "
        f"{len(order_ids) - len(new_ids)} already present."
    )


if __name__ == "__main__":
    main()

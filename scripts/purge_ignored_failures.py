"""
One-off helper: deletes rows from sync_failures whose (platform,
source_order_id) is ALSO already present in ignored_orders.

Motivating case (2026-09): sync_engine.py's retry_pending_failures() used
to retry every row in sync_failures unconditionally, with no check
against the ignored_orders permanent skip-list - see the fix in
sync_engine.py (retry_pending_failures) and repository.py
(Repository.clear_failure) for the full story. That fix stops the bug
going forward, but it does nothing for orders that ALREADY have a stale
sync_failures row sitting in the live DB from before the fix (e.g. every
digikala order seeded into ignored_orders via seed_ignored_orders.py
that had already failed at least once beforehand) - those rows just sit
there and get evaluated (and dropped) on the very next retry pass rather
than on every single one, but running this script clears the backlog
immediately instead of waiting for that.

Run from the project root (with the venv activated):
    python -m scripts.purge_ignored_failures
    python -m scripts.purge_ignored_failures --platform digikala
    python -m scripts.purge_ignored_failures --dry-run
"""
from __future__ import annotations

import argparse

from src.db.repository import Repository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        default=None,
        help="Only purge this platform (default: all platforms present in sync_failures)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without actually deleting anything",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override DB_PATH (defaults to whatever src.config.settings uses)",
    )
    args = parser.parse_args()

    repo = Repository(db_path=args.db_path)

    with repo._connect() as conn:
        platforms = (
            [args.platform]
            if args.platform
            else [row[0] for row in conn.execute("SELECT DISTINCT platform FROM sync_failures").fetchall()]
        )

    total_purged = 0
    for platform in platforms:
        ignored_ids = repo.get_ignored_ids(platform)
        if not ignored_ids:
            continue

        with repo._connect() as conn:
            rows = conn.execute(
                "SELECT source_order_id FROM sync_failures WHERE platform = ?",
                (platform,),
            ).fetchall()
        stuck_ids = [row[0] for row in rows if row[0] in ignored_ids]

        if not stuck_ids:
            print(f"purge_ignored_failures: platform={platform!r} - nothing stuck, skipping.")
            continue

        if args.dry_run:
            print(
                f"purge_ignored_failures: platform={platform!r} - would purge "
                f"{len(stuck_ids)} stale failure row(s) (dry run, nothing deleted)."
            )
        else:
            for source_order_id in stuck_ids:
                repo.clear_failure(platform, source_order_id)
            print(
                f"purge_ignored_failures: platform={platform!r} - purged "
                f"{len(stuck_ids)} stale failure row(s)."
            )
        total_purged += len(stuck_ids)

    if total_purged == 0:
        print("purge_ignored_failures: nothing to do.")
    elif args.dry_run:
        print(f"purge_ignored_failures: {total_purged} row(s) total would be purged. Re-run without --dry-run to apply.")
    else:
        print(f"purge_ignored_failures: {total_purged} row(s) total purged.")


if __name__ == "__main__":
    main()

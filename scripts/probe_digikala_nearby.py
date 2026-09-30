"""
Manual, READ-ONLY live check for the 3-hour order support in
src/marketplaces/digikala_nearby.py (GET /open-api/v1/near-by-stores).

WHY THIS IS A SCRIPT AND NOT A pytest TEST
------------------------------------------
tests/test_digikala_nearby.py proves the mixin's own logic against a fake.
It cannot prove the two things the implementation still guesses, because
they need a REAL payload from Digikala:

  1. Does a `pending` row already carry the customer's name/mobile/address,
     or are those null until the order is confirmed?
  2. Does `search[search_term]=<shipmentId>` really narrow the result (the
     retry / detail path depends on it), or is it silently ignored?

This script answers both on your real accounts and also dry-runs the exact
production code path (cold start, then "one new order") without touching
your real database or Didar.

WHAT IT NEVER DOES
------------------
  * never calls update-status (NEARBY_AUTO_CONFIRM is forced to False and
    _update_status is replaced with a function that raises);
  * never talks to Didar, Telegram or the SMS panel;
  * never writes to data/sync.db: the adapters get a Repository on a
    throw-away temp file. (The Digikala token cache files are shared with
    the live service on purpose - see scripts/probe_digikala_warehouse.py
    for why; a refresh here is safe and uses the adapters' own logic.)

Read-only GETs it does make: near-by-stores, and - only in Section C, for
the ONE simulated new row - the Promotions API lookup that normalizing a
row performs (that endpoint has a low rate limit; a handful of calls).

HOW TO RUN (project root, venv activated)
-----------------------------------------
    python -m scripts.probe_digikala_nearby                    # both accounts
    python -m scripts.probe_digikala_nearby --account digikala2
    python -m scripts.probe_digikala_nearby --shipment 385944895
    python -m scripts.probe_digikala_nearby --redact --dump nearby_probe.json

  --redact   masks customer name/phone/address/postal/email/verification
             values in the printed JSON (null stays null, so you can still
             see WHICH fields are empty). Use it before pasting output.
  --dump F   also writes the (redacted if --redact) raw payloads to file F.
  --no-normalize  skip Section C's normalization (no Promotions API calls).

Tip: run it right after registering a real 3-hour order in the panel, while
that order is still open, so Section A has a real row to show.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_SENSITIVE_TOKENS = (
    "customer",
    "mobile",
    "phone",
    "address",
    "postal",
    "email",
    "verification",
    "receiver",
    "recipient",
    "national",
)


def redact(value):
    """Mask the value of every sensitive-looking key; None stays None so
    'null vs filled' remains visible."""
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if any(tok in str(key).lower() for tok in _SENSITIVE_TOKENS) and not isinstance(
                val, (dict, list)
            ):
                out[key] = None if val is None else "***"
            else:
                out[key] = redact(val)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _show(obj, do_redact: bool) -> str:
    return json.dumps(redact(obj) if do_redact else obj, ensure_ascii=False, indent=2, default=str)


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _customer_fields(row: dict) -> dict:
    """Every top-level key that looks customer-related, with just
    filled/null - answers 'does pending carry customer data?' at a glance."""
    found = {}
    for key, val in row.items():
        if any(t in key.lower() for t in ("customer", "mobile", "phone", "address", "postal", "email")):
            found[key] = "NULL" if val in (None, "", {}, []) else "filled"
    return found


# --------------------------------------------------------------------------
# Section A - raw open rows per status
# --------------------------------------------------------------------------
def section_a(adapter, do_redact: bool, dump: dict) -> list[dict]:
    _hr(f"[{adapter.name}] SECTION A - open 3-hour rows (raw, page 1 per status)")
    all_rows: list[dict] = []
    for status in ("pending", "processing"):
        params = {"page": 1, "size": 50, "sort": "id", "order": "asc", "search[status]": status}
        try:
            payload = adapter._get("/open-api/v1/near-by-stores", params=params)
        except Exception as exc:  # show the body Digikala sent, that IS the finding
            print(f"\nstatus={status}: REQUEST FAILED -> {exc!r}")
            print("  (403 = this token has no access to near-by-stores; 422 = a parameter is rejected)")
            continue
        data = payload.get("data") or {}
        items = data.get("items") or []
        print(f"\nstatus={status}: {len(items)} row(s); pager={data.get('pager')!r}")
        dump.setdefault(adapter.name, {})[status] = payload
        for item in items:
            all_rows.append(item)
            sid = item.get("shipmentId")
            status_text = (item.get("status") or {}).get("text")
            print(
                f"  - shipmentId={sid} status={status_text!r} "
                f"isDigiExpress={item.get('isDigiExpress')!r} "
                f"nextStatus={item.get('nextStatus')!r} "
                f"isCancelled={item.get('isCancelled')!r}"
            )
            print(f"    customer-ish fields: {_customer_fields(item)}")
        if items:
            print(f"\n  first row, full JSON ({status}):")
            print("  " + _show(items[0], do_redact).replace("\n", "\n  "))
    if not all_rows:
        print("\nNo open 3-hour rows right now. Register/receive a 3-hour order and re-run;")
        print("Q1 (customer data on pending) cannot be answered from an empty list.")
    return all_rows


# --------------------------------------------------------------------------
# Section B - does search[search_term] really filter?
# --------------------------------------------------------------------------
def section_b(adapter, rows: list[dict], shipment: str | None) -> None:
    _hr(f"[{adapter.name}] SECTION B - search[search_term] behaviour")
    target = shipment or (str(rows[0]["shipmentId"]) if rows else None)
    if target is None:
        print("No shipment id to test with (no open rows and no --shipment). Skipped.")
        return
    print(f"target shipmentId = {target}")

    try:
        found = adapter._fetch_nearby_row(target)
    except Exception as exc:
        print(f"_fetch_nearby_row raised: {exc!r}")
        return
    print(f"_fetch_nearby_row({target}) -> {'FOUND' if found else 'NOT FOUND (empty dict)'}")

    # Control: a bogus term. If the server IGNORES search_term, this returns
    # the same rows as an unfiltered call, and the 'found' result above
    # proves nothing (it would only mean the row is in the open list).
    try:
        bogus = adapter._get(
            "/open-api/v1/near-by-stores",
            params={"page": 1, "size": 50, "search[search_term]": "0000000001"},
        )
        plain = adapter._get("/open-api/v1/near-by-stores", params={"page": 1, "size": 50})
    except Exception as exc:
        print(f"control request failed: {exc!r}")
        return
    n_bogus = len((bogus.get("data") or {}).get("items") or [])
    n_plain = len((plain.get("data") or {}).get("items") or [])
    print(f"control: rows with bogus term = {n_bogus}, rows with no term = {n_plain}")
    if n_plain == 0:
        print("  VERDICT: INCONCLUSIVE - the unfiltered list is empty, so the control proves")
        print("           nothing. Also suggests near-by-stores lists ONLY open orders, so a")
        print(f"           closed shipment ({target}) cannot be found by any search term.")
        print("           Re-run while an open 3-hour order exists.")
    elif n_bogus == n_plain:
        print("  VERDICT: search_term is IGNORED server-side. _fetch_nearby_row still works")
        print("           (it re-checks shipmentId client-side) but only for rows on page 1;")
        print("           a closed/old shipment will NOT be found by the retry path.")
    elif n_bogus == 0:
        print("  VERDICT: search_term FILTERS server-side (bogus term -> 0 rows).")
    else:
        print("  VERDICT: inconclusive - inspect by hand.")


# --------------------------------------------------------------------------
# Section C - dry run of the production path on a throw-away DB
# --------------------------------------------------------------------------
def section_c(adapter, normalize: bool) -> None:
    _hr(f"[{adapter.name}] SECTION C - dry run of fetch_new_nearby_orders (temp DB)")
    repo = adapter._repo

    first = adapter._fetch_new_nearby_orders()
    ignored = repo.get_ignored_ids(adapter.name)
    print(f"poll 1 (cold start): returned {len(first)} order(s) [expected 0]")
    print(f"  ignored ids now   : {sorted(ignored)}")
    print(f"  cold-start marker : {repo.get_last_shipment_id(adapter.name + '_nearby')}")
    if first:
        print("  !! cold start returned orders - that would flood Didar. BUG.")

    second = adapter._fetch_new_nearby_orders()
    print(f"poll 2 (nothing new): returned {len(second)} order(s) [expected 0]")
    if second:
        print("  !! second poll returned orders that were just ignored. BUG.")

    if not ignored:
        print("\nNo open rows were ignored, so there is nothing to 'un-ignore' for the")
        print("simulated-new-order step. Re-run while a 3-hour order is open.")
        return

    newest = str(max(int(i) for i in ignored))
    with sqlite3.connect(repo._db_path) as conn:
        conn.execute(
            "DELETE FROM ignored_orders WHERE platform = ? AND source_order_id = ?",
            (adapter.name, newest),
        )
    print(f"\nsimulating a NEW order: removed {newest} from the temp ignore list")

    if not normalize:
        print("--no-normalize given: skipping the poll that normalizes it.")
        return

    third = adapter._fetch_new_nearby_orders()
    print(f"poll 3: returned {len(third)} order(s) [expected 1]")
    for order in third:
        print(f"  source_order_id  = {order.source_order_id}")
        print(f"  source           = {order.source}   [expected {adapter.name}]")
        print(f"  shipping_method  = {order.shipping_method!r}   [expected 'EXPRESS']")
        print(f"  status           = {order.status!r}")
        print(f"  created_at       = {order.created_at!r}")
        print(f"  total_price      = {order.total_price}")
        print(f"  customer name/mob= {order.customer_full_name!r} / {order.customer_mobile!r}")
        print(f"  address          = {order.customer_address!r}")
        print(f"  items            = {len(order.items)}")
        for item in order.items:
            print(f"    - {item!r}")
    fourth = adapter._fetch_new_nearby_orders()
    print(f"poll 4 (same order again): returned {len(fourth)} order(s) [expected 0 - in-process dedup]")


# --------------------------------------------------------------------------
def _build_adapter(account: str, db_path: str):
    from src.config import settings
    from src.db.repository import Repository

    cfg = settings.digikala2 if account == "digikala2" else settings.digikala
    if not cfg.base_url or not (cfg.access_token or cfg.refresh_token):
        print(f"[{account}] skipped: base_url / tokens are not configured in .env")
        return None
    repo = Repository(db_path=db_path)
    if account == "digikala2":
        from src.marketplaces.digikala2 import Digikala2Adapter

        adapter = Digikala2Adapter(repository=repo)
    else:
        from src.marketplaces.digikala import DigikalaAdapter

        adapter = DigikalaAdapter(repository=repo)

    # Safety rails: this script must never confirm an order.
    adapter.NEARBY_AUTO_CONFIRM = False

    def _forbidden(*_a, **_k):
        raise RuntimeError("probe_digikala_nearby must never call update-status")

    adapter._update_status = _forbidden
    return adapter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--account", choices=["digikala", "digikala2", "both"], default="both")
    parser.add_argument("--shipment", help="a specific shipmentId to test search_term with")
    parser.add_argument("--redact", action="store_true", help="mask customer fields in printed JSON")
    parser.add_argument("--dump", help="write raw payloads (redacted if --redact) to this file")
    parser.add_argument("--no-normalize", action="store_true", help="skip Section C normalization")
    args = parser.parse_args()

    accounts = ["digikala", "digikala2"] if args.account == "both" else [args.account]
    dump: dict = {}
    print(f"# near-by-stores probe - {datetime.now().isoformat(timespec='seconds')}")

    with tempfile.TemporaryDirectory() as tmp:
        for account in accounts:
            adapter = _build_adapter(account, str(Path(tmp) / f"{account}.db"))
            if adapter is None:
                continue
            rows = section_a(adapter, args.redact, dump)
            section_b(adapter, rows, args.shipment)
            try:
                section_c(adapter, normalize=not args.no_normalize)
            except Exception as exc:
                print(f"\nSection C failed: {exc!r}")
                import traceback

                traceback.print_exc()

    if args.dump:
        Path(args.dump).write_text(
            json.dumps(redact(dump) if args.redact else dump, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nraw payloads written to {args.dump}" + (" (redacted)" if args.redact else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
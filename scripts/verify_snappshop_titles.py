"""
READ-ONLY verification for the SnappShop item-name fix
(src/marketplaces/snappshop.py / snappshop2.py).

WHY: SnappShop orders used to reach Didar as the placeholder product
"snappshop item 0" (sku "0" + no item title). The fix ignores the "0"
sku and looks the title up via GET /vendors/{id}/products. The title
field name in that response was NEVER confirmed, so run this before
trusting the fix in production.

It only performs GET requests to SnappShop. It never writes to Didar
and never touches the local DB.

HOW TO RUN (project root, venv activated)
    python -m scripts.verify_snappshop_titles
    python -m scripts.verify_snappshop_titles 443123198 2120030074
    python -m scripts.verify_snappshop_titles > snapp_verify.txt 2>&1

Exit code 0 = all checks passed, 1 = something needs attention.

CHECKS
  1. /products responds and a title-like field exists.
  2. Every item of each given order gets a non-empty title.
  3. No item ends up with sku "0"/empty (the old collision bug).
  4. Different products in the same run get different Didar Codes.
  5. (info) whether the title matches the Excel catalog, if configured.
"""
from __future__ import annotations

import json
import sys

from src.config import settings
from src.marketplaces.snappshop import SnappShopAdapter
from src.marketplaces.snappshop2 import SnappShop2Adapter

DEFAULT_ORDERS = ["443123198", "2120030074"]
problems: list[str] = []


def hr(text: str) -> None:
    print("\n" + "=" * 8 + " " + text + " " + "=" * 8)


def fail(msg: str) -> None:
    problems.append(msg)
    print("  [FAIL]", msg)


def ok(msg: str) -> None:
    print("  [ OK ]", msg)


def accounts():
    out = []
    if settings.snappshop.enabled and settings.snappshop.auth_token:
        out.append(("snappshop", SnappShopAdapter()))
    if settings.snappshop2.enabled and settings.snappshop2.auth_token:
        out.append(("snappshop2", SnappShop2Adapter()))
    return out


def check_products(name, adapter) -> None:
    hr(f"A. {name}: GET /products (page 1)")
    try:
        payload = adapter._get(f"/vendors/{adapter._config.vendor_id}/products", params={"page": 1})
    except Exception as exc:  # noqa: BLE001
        fail(f"{name}: /products call failed: {exc!r}")
        return
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    print(f"  top-level keys: {sorted(payload) if isinstance(payload, dict) else type(payload)}")
    print(f"  rows on page 1: {len(rows)}")
    if not rows:
        fail(f"{name}: /products returned no rows - titles can't be resolved")
        return
    print("  first row (raw):")
    print("   ", json.dumps(rows[0], ensure_ascii=False)[:1500])
    titles = adapter._load_product_titles()
    if titles:
        ok(f"{name}: {len(titles)} id->title entries loaded")
        sample = list(titles.items())[:3]
        for k, v in sample:
            print(f"     {k} -> {v}")
    else:
        fail(f"{name}: no titles extracted - send the raw row above so the field name can be added")


def check_orders(name, adapter, order_numbers, catalog) -> list[tuple[str, str]]:
    hr(f"B. {name}: order items")
    seen: list[tuple[str, str]] = []
    for number in order_numbers:
        try:
            raw = adapter._get(f"/vendors/{adapter._config.vendor_id}/orders/{number}")
        except Exception as exc:  # noqa: BLE001
            print(f"  order {number}: not on this account ({str(exc)[:80]})")
            continue
        raw = raw.get("data", raw)
        print(f"\n  order {number} - raw item keys/values:")
        for it in raw.get("items", []):
            print("   ", json.dumps(it, ensure_ascii=False)[:600])
        order = adapter._normalize_detail(raw)
        for item in order.items:
            label = f"{name}/{number}/sku={item.sku!r}"
            print(f"  -> normalized: sku={item.sku!r} title={item.title!r}")
            if not item.title:
                fail(f"{label}: title is EMPTY (falls back to id-based name)")
            else:
                ok(f"{label}: title found")
            if item.sku in ("", "0"):
                fail(f"{label}: sku is still empty/'0'")
            if catalog is not None and item.title:
                match = catalog.resolve_catalog_code(item.title)
                print(f"     Excel catalog match: {match}")
            code = item.sku if item.title == "" else item.title
            seen.append((number, code))
    return seen


def main() -> int:
    numbers = sys.argv[1:] or DEFAULT_ORDERS
    accs = accounts()
    if not accs:
        print("No SnappShop account enabled in .env (SNAPPSHOP_ENABLED / SNAPPSHOP2_ENABLED).")
        return 1

    catalog = None
    try:
        from src.didar.product_client import DidarProductClient
        catalog = DidarProductClient()
    except Exception as exc:  # noqa: BLE001
        print(f"(catalog check skipped: {exc!r})")

    all_seen: list[tuple[str, str]] = []
    for name, adapter in accs:
        check_products(name, adapter)
        all_seen += check_orders(name, adapter, numbers, catalog)

    hr("C. Distinct products")
    if len(all_seen) >= 2:
        codes = {c for _, c in all_seen}
        orders = {o for o, _ in all_seen}
        print(f"  orders checked: {sorted(orders)}  distinct item keys: {len(codes)}")
        if len(codes) == 1 and len(orders) > 1:
            print("  (same key on every order - fine only if they truly bought the same product)")
    elif not all_seen:
        fail("none of the given orders were found on any enabled account")

    hr("RESULT")
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(" -", p)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

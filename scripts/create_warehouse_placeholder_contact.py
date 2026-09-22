"""
One-off helper: creates (or finds, if already created) the dedicated
placeholder Contact used for every digikala_warehouse (FBD) Deal, and
prints its Id so it can be pasted into .env as
DIDAR_WAREHOUSE_PLACEHOLDER_PERSON_ID.

WHY A DEDICATED CONTACT, NOT A STAFF MEMBER'S OWN PERSON RECORD:
the FBD source endpoint exposes no customer at all, but Didar's
Deal.save_v2 contract requires PersonId (confirmed: a Deal with neither
PersonId nor CompanyId is rejected with "person and company both are
empty", and per Didar's own docs PersonId is Required for Create Deal -
CompanyId alone does not satisfy it). Using a real staff member's
Person record here would mix FBD deals into that person's own contact
history in Didar; a dedicated Contact keeps them cleanly separate and
searchable as their own thing.

This reuses the project's own DidarContactClient.upsert_contact() -
same call every normal order's Contact goes through - keyed on a fixed
CustomerCode so running this script again (e.g. after a Didar data
cleanup) finds the SAME Contact instead of creating a duplicate.

Reads DIDAR_BASE_URL / DIDAR_API_KEY straight from the project's own
.env via src.config, so there's nothing to paste in by hand.

Run from the project root (with the venv activated):
    python -m scripts.create_warehouse_placeholder_contact

Then copy the printed Id into .env:
    DIDAR_WAREHOUSE_PLACEHOLDER_PERSON_ID=<the Id printed below>
"""
from __future__ import annotations

from src.config import settings
from src.didar.contact_client import DidarContactClient

# Fixed and distinctive on purpose - never expected to collide with a
# real marketplace customer_code (those are numeric order/customer ids
# from Digikala/Basalam/etc).
_PLACEHOLDER_CUSTOMER_CODE = "fbd-warehouse-placeholder"
_PLACEHOLDER_FULL_NAME = "سفارشات انبار دیجی‌کالا"


def main() -> None:
    cfg = settings.didar
    if not cfg.base_url or not cfg.api_key:
        print("DIDAR_BASE_URL / DIDAR_API_KEY are not set in .env - fill those in first.")
        return

    if cfg.warehouse_placeholder_person_id:
        print(
            "DIDAR_WAREHOUSE_PLACEHOLDER_PERSON_ID is already set in .env to "
            f"{cfg.warehouse_placeholder_person_id!r} - nothing to do. Delete it "
            "from .env first if you deliberately want to create a new/different "
            "placeholder Contact."
        )
        return

    client = DidarContactClient(cfg)
    result = client.upsert_contact(
        customer_code=_PLACEHOLDER_CUSTOMER_CODE,
        full_name=_PLACEHOLDER_FULL_NAME,
    )

    print(f"Contact ready: DisplayName={result.display_name!r} Id={result.id}")
    print()
    print("Add this line to .env:")
    print(f"DIDAR_WAREHOUSE_PLACEHOLDER_PERSON_ID={result.id}")


if __name__ == "__main__":
    main()

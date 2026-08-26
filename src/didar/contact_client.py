"""
Didar CRM - Contact client.

Endpoint confirmed via Didar's own official API guide (didar.me/api-help)
combined with a third-party integration listing (apieco.ir/api/didar-crm)
that documents the exact request shape for the same underlying API:

    POST {DIDAR_BASE_URL}/contact/save?apikey={API_KEY}
    body: {"Contact": {"CustomerCode": ..., "FirstName": ..., "Lastname": ...,
                        "MobilePhone": ..., ...}}

Authentication: confirmed from didar.me/api-help itself - the API key is
passed as a QUERY STRING parameter on every call, not an Authorization
header.

UPSERT CORRECTION (2026-08, production incident): the original version
of this module claimed "confirmed both from docs and live testing" that
Didar auto-upserts by CustomerCode alone - i.e. that /contact/save could
just be called blind, with no Id, and Didar would update an existing
Contact if CustomerCode matched. A real production run proved that
WRONG: a second order from a repeat Faraz Honar customer (same phone
number, so the same CustomerCode - see service.py's _customer_code_for())
failed with a 400 "Duplicate contacts is not allowed", not a silent
update. Whatever test originally "confirmed" the upsert must only have
exercised brand-new customers, where there was nothing to conflict with.

FIX: search-first, same pattern already used in product_client.py
(search-by-code before /product/save) and deal_client.py
(find_existing_deal_id before /deal/save). find_existing_contact_id()
uses the documented global search endpoint POST /search/search
(Types=["contact"]) to look up an existing Contact by CustomerCode
before ever calling /contact/save; if one is found, its Id is included
in the request body so Didar treats the call as an edit. Only when
nothing is found does the call omit Id and create fresh. A residual
race (another process creates the same CustomerCode between our search
and this save) is recovered the same way product_client.py handles its
"duplicate product code" race - one retry-via-search rather than
failing the whole order for a timing issue.

Response envelope ({"Response": {"Contact": {...}}}) is confirmed via
live testing. upsert_contact() returns a ContactResult carrying both
the Id (needed for Deal.PersonId) and the DisplayName Didar computed
(needed for Deal.Title = "معامله {display_name}", matching Didar's own
default naming convention for manually-created deals).
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from src.config import DidarConfig, settings
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

log = get_logger(__name__)


class DidarApiError(RuntimeError):
    """Raised when Didar's response doesn't indicate success, or its
    shape doesn't match any of the expected patterns."""


@dataclass(frozen=True)
class ContactResult:
    id: str
    display_name: str


class DidarContactClient:
    def __init__(self, config: DidarConfig | None = None) -> None:
        self._config = config or settings.didar
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json)
        raise_for_status_with_body(resp)
        return resp.json()

    def find_existing_contact_id(self, customer_code: str, limit: int = 20) -> str | None:
        """
        Look up an existing Contact by CustomerCode via the documented
        global search endpoint (POST /search/search, Types=["contact"]).
        Full-text search, not an exact-CustomerCode filter (same
        situation as /product/search's Keywords), so results are
        filtered down to an exact match here - a wrong partial match
        would silently attach this order's Deal to the wrong Contact,
        which is worse than not finding one at all.
        """
        payload = self._post(
            "/search/search", json={"Keyword": customer_code, "Types": ["contact"]}
        )
        results = payload.get("Response", {}).get("List", [])
        for item in results:
            if not isinstance(item, dict) or item.get("_tp") != "contact":
                continue
            if item.get("CustomerCode") == customer_code:
                contact_id = item.get("Id")
                if contact_id:
                    return str(contact_id)
        return None

    def upsert_contact(
        self,
        customer_code: str,
        mobile_phone: str | None = None,
        full_name: str | None = None,
    ) -> ContactResult:
        """
        Create-or-update a Contact keyed on customer_code, returning its
        Didar Contact Id and DisplayName. Searches for an existing
        Contact FIRST (see module docstring for why - /contact/save does
        NOT reliably auto-upsert by CustomerCode alone) and includes its
        Id in the save body when found, so Didar treats the call as an
        edit rather than rejecting it as a duplicate create.
        """
        first_name, last_name = _split_name(full_name)
        if not last_name:
            # Confirmed via a live 400 ("LastName can not be empty"):
            # Didar requires a non-empty LastName. Sources that don't
            # provide a customer name at all (Tapsi Shop, Digikala - see
            # their adapters' module docstrings) would otherwise send
            # both fields empty; a single-word name (e.g. "Ali") hits
            # the same problem since _split_name leaves last_name "".
            # customer_code is always non-empty and unique per order,
            # so it's a safe, still-identifiable fallback.
            first_name = first_name or "مشتری"
            last_name = customer_code

        contact_body = {
            "CustomerCode": customer_code,
            "FirstName": first_name,
            "Lastname": last_name,
            "MobilePhone": mobile_phone or "",
        }

        existing_id = self.find_existing_contact_id(customer_code)
        if existing_id:
            contact_body["Id"] = existing_id

        try:
            payload = self._post("/contact/save", json={"Contact": contact_body})
        except httpx.HTTPStatusError as exc:
            # Race: search found nothing, but the Contact was created by
            # something else (another sync run, a near-simultaneous
            # retry) between our search and this save. Recover by
            # searching once more rather than failing the whole order
            # for a timing issue - same pattern as
            # product_client.py's "duplicate product code" recovery.
            if (
                not existing_id
                and exc.response is not None
                and "duplicate" in exc.response.text.lower()
            ):
                recovered_id = self.find_existing_contact_id(customer_code)
                if recovered_id:
                    contact_body["Id"] = recovered_id
                    payload = self._post("/contact/save", json={"Contact": contact_body})
                else:
                    raise
            else:
                raise

        result = _extract_contact_result(payload)
        log.info("didar: upserted contact CustomerCode=%s -> Id=%s", customer_code, result.id)
        return result


def _split_name(full_name: str | None) -> tuple[str, str]:
    if not full_name:
        return "", ""
    parts = full_name.strip().split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _extract_contact_result(payload: dict) -> ContactResult:
    # Confirmed shape (see module docstring); a couple of flatter
    # fallbacks kept as a safety net only.
    candidates = [
        lambda p: p.get("Response", {}).get("Contact", {}),
        lambda p: p.get("Response", {}),
        lambda p: p.get("Contact", {}),
        lambda p: p,
    ]
    for get in candidates:
        try:
            contact = get(payload)
        except AttributeError:
            continue
        contact_id = contact.get("Id") if isinstance(contact, dict) else None
        if contact_id:
            display_name = contact.get("DisplayName") or ""
            return ContactResult(id=str(contact_id), display_name=display_name)

    raise DidarApiError(
        f"didar: could not find Contact Id in response - shape is unconfirmed, "
        f"update _extract_contact_result() once a real payload has been inspected. "
        f"Raw response: {payload!r}"
    )

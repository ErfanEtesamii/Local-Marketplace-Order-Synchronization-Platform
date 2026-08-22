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

Upsert behavior: confirmed both from docs and live testing - if
CustomerCode matches an existing Contact, Didar updates it; otherwise
it creates a new one. This is what lets every marketplace adapter just
call upsert_contact() on every order without a separate "does this
customer already exist" lookup.

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

    def upsert_contact(
        self,
        customer_code: str,
        mobile_phone: str | None = None,
        full_name: str | None = None,
    ) -> ContactResult:
        """
        Create-or-update a Contact keyed on customer_code, returning its
        Didar Contact Id and DisplayName.
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

        body = {
            "Contact": {
                "CustomerCode": customer_code,
                "FirstName": first_name,
                "Lastname": last_name,
                "MobilePhone": mobile_phone or "",
            }
        }
        payload = self._post("/contact/save", json=body)
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

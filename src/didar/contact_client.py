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

Upsert behavior: per the Postman docs captured earlier in this project,
if CustomerCode matches an existing Contact, Didar updates it; otherwise
it creates a new one. This is what lets every marketplace adapter just
call upsert_contact() on every order without a separate "does this
customer already exist" lookup.

NOT YET CONFIRMED: the exact shape of the response envelope (which key
the new/updated Contact's Id is nested under). _extract_contact_id()
below tries several plausible shapes defensively and logs a loud error
with the raw payload if none match, rather than guessing silently -
this must be verified against a real API call once DIDAR_API_KEY is
available.
"""
from __future__ import annotations

import httpx

from src.config import DidarConfig, settings
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

log = get_logger(__name__)


class DidarApiError(RuntimeError):
    """Raised when Didar's response doesn't indicate success, or its
    shape doesn't match any of the expected patterns."""


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
    ) -> str:
        """
        Create-or-update a Contact keyed on customer_code, returning its
        Didar Contact Id (needed for Deal.ContactId).
        """
        first_name, last_name = _split_name(full_name)

        body = {
            "Contact": {
                "CustomerCode": customer_code,
                "FirstName": first_name,
                "Lastname": last_name,
                "MobilePhone": mobile_phone or "",
            }
        }
        payload = self._post("/contact/save", json=body)
        contact_id = _extract_contact_id(payload)
        log.info("didar: upserted contact CustomerCode=%s -> Id=%s", customer_code, contact_id)
        return contact_id


def _split_name(full_name: str | None) -> tuple[str, str]:
    if not full_name:
        return "", ""
    parts = full_name.strip().split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _extract_contact_id(payload: dict) -> str:
    # Try the plausible response shapes, most-likely first. See module
    # docstring - this is the one piece that needs live-token verification.
    candidates = [
        lambda p: p.get("Response", {}).get("Contact", {}).get("Id"),
        lambda p: p.get("Response", {}).get("Id"),
        lambda p: p.get("Contact", {}).get("Id"),
        lambda p: p.get("Id"),
    ]
    for get in candidates:
        try:
            value = get(payload)
        except AttributeError:
            continue
        if value:
            return str(value)

    raise DidarApiError(
        f"didar: could not find Contact Id in response - shape is unconfirmed, "
        f"update _extract_contact_id() once a real payload has been inspected. "
        f"Raw response: {payload!r}"
    )

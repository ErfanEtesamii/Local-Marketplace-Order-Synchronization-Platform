"""
Shared HTTP retry policy for every external API client in this project
(marketplace adapters and the Didar CRM client).

Centralized here specifically because the retry predicate used to be
duplicated in each adapter, and one bug (see git history: "fix: correct
retry predicate wiring...") slipped into all three copies at once because
of that duplication. A single shared implementation means a future fix
only has to happen in one place.
"""
from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception


def is_retryable_http_error(exc: BaseException) -> bool:
    """
    Only retry on server errors / rate limiting - never on 4xx client
    errors (validation, expired auth), which need human intervention
    rather than automatic retries.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TransportError)


def raise_for_status_with_body(resp: httpx.Response) -> None:
    """
    Like httpx.Response.raise_for_status(), but includes the response
    body in the raised exception's message. httpx's default leaves the
    body out, which meant a real 400 from Didar (e.g. "MobilePhone
    format invalid") only showed a bare status code in the traceback -
    the actual reason had to be reproduced manually to see at all.
    """
    if resp.is_success:
        return
    message = (
        f"{resp.status_code} {resp.reason_phrase} for url '{resp.url}': {resp.text}"
    )
    raise httpx.HTTPStatusError(message, request=resp.request, response=resp)


def default_retry():
    """
    Standard retry decorator used by every external API client here:
    up to 3 attempts, exponential backoff (1s, 2s, 4s, capped at 10s).
    """
    return retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(is_retryable_http_error),
    )

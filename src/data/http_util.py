from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    **kwargs,
) -> httpx.Response:
    """Issue a request, retrying transport errors and 5xx responses.

    Waits ``backoff * 2**n`` seconds between attempts. The final failure is
    re-raised (or the non-retryable response returned) so callers keep their
    existing error handling.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last_exc = exc
            logger.warning(
                "%s %s failed (attempt %d/%d): %s", method, url, attempt + 1, attempts, exc
            )
        else:
            if resp.status_code >= 500 and attempt < attempts - 1:
                logger.warning(
                    "%s %s returned %d (attempt %d/%d)",
                    method, url, resp.status_code, attempt + 1, attempts,
                )
            else:
                return resp
        if attempt < attempts - 1:
            await asyncio.sleep(backoff * (2**attempt))
    if last_exc is not None:
        raise last_exc
    return resp

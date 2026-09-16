"""Stream package downloads through the local nginx cache."""

from __future__ import annotations

import hashlib
import httpx
import logging
from repowatch.bandwidth import BandwidthLimiter
from repowatch.parsers.base import USER_AGENT

logger = logging.getLogger(__name__)


# Chunk size for streaming the response body in download_package — small enough for
# accurate byte-level pacing, large enough not to flood syscalls.
_CHUNK_SIZE = 64 * 1024


async def download_package(
    client: httpx.AsyncClient, url: str, limiter: BandwidthLimiter,
    *, expected_sha256: str | None = None, expected_size: int | None = None,
) -> tuple[bool, int | None]:
    """The body is read in chunks (not loaded into memory whole) — each
    chunk "spends" its size in the limiter, so the limit is actually paced
    by bytes that went over the network, not by request count.

    Unlike urllib, httpx doesn't raise on a non-2xx status by itself —
    raise_for_status() runs right after headers arrive (before streaming
    the body), so the bandwidth budget isn't spent on an error page's body."""
    try:
        async with client.stream(
            "GET", url, headers={"User-Agent": USER_AGENT}, timeout=60
        ) as resp:
            resp.raise_for_status()
            digest = hashlib.sha256() if expected_sha256 else None
            received = 0
            async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                received += len(chunk)
                await limiter.consume(len(chunk))
                if expected_size is not None and received > expected_size:
                    logger.warning("warm size exceeds metadata: %s", url)
                    return False, resp.status_code
                if digest is not None:
                    digest.update(chunk)
            if expected_size is not None and received != expected_size:
                logger.warning("warm size mismatch: %s", url)
                return False, resp.status_code
            if digest is not None and digest.hexdigest() != expected_sha256.lower():
                logger.warning("warm hash mismatch: %s", url)
                return False, resp.status_code
            logger.info("warmed: %s (%s)", url, resp.status_code)
            return True, resp.status_code
    except httpx.HTTPStatusError as exc:
        logger.warning("warm failed (HTTP %s): %s", exc.response.status_code, url)
        return False, exc.response.status_code
    except httpx.RequestError as exc:
        logger.warning("warm failed (%s): %s", exc, url)
        return False, None

"""Global HTTP request logging via a ``requests.Session.send`` monkey-patch.

Every outbound ``requests.get/post/put/...`` call ultimately flows through
``Session.send``. Wrapping that single seam logs *all* HTTP traffic from
the ``requests`` library at one place — lyrics fetchers (LRCLib, Spotify,
Genius, Tekstowo), iTunes/MusicBrainz, YouTube Data API, etc.

Cookies and ``Authorization`` headers are intentionally not logged.

``install()`` is idempotent — calling it twice is a no-op.
"""

import logging
import time

import requests

logger = logging.getLogger("pikaraoke.http")

_INSTALLED = False
_ORIGINAL_SEND = None


def _format_url(prepared: requests.PreparedRequest) -> str:
    """Return ``METHOD URL`` for logging. The PreparedRequest already has
    the query string baked into ``url``, so we don't need to re-stringify
    params separately."""
    return f"{prepared.method} {prepared.url}"


def _patched_send(self, request, **kwargs):
    started = time.monotonic()
    label = _format_url(request)
    try:
        response = _ORIGINAL_SEND(self, request, **kwargs)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("HTTP %s FAILED (%dms): %s", label, elapsed_ms, exc)
        raise
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("HTTP %s -> %s (%dms)", label, response.status_code, elapsed_ms)
    return response


def install() -> None:
    """Monkey-patch ``requests.Session.send`` to log every outbound HTTP call.

    Safe to call multiple times.
    """
    global _INSTALLED, _ORIGINAL_SEND
    if _INSTALLED:
        return
    _ORIGINAL_SEND = requests.Session.send
    requests.Session.send = _patched_send  # type: ignore[method-assign]
    _INSTALLED = True

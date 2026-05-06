"""Spotify TOTP secret loader + 6-digit code generator.

Spotify's ``/api/token`` endpoint requires a TOTP code generated from a
rotating secret. The secret cipher is published by the community at
``xyloflake/spot-secrets-go`` and refreshed when Spotify rotates it. We
fetch once per process (24h TTL), cache in memory, and graceful-degrade
by raising :class:`SpotifyTOTPError` when fetch fails — the caller
treats that as "Spotify backend unavailable" and disables the variant.

Reference: ``akashrchandran/syrics`` (``syrics/totp.py``).
"""

import hashlib
import hmac
import logging
import math
import threading
import time

import requests

logger = logging.getLogger(__name__)

SECRET_CIPHER_DICT_URL = (
    "https://raw.githubusercontent.com/xyloflake/spot-secrets-go/main/secrets/secretDict.json"
)
SECRET_FETCH_TIMEOUT = 5.0
# Rotates every few days; daily refresh is enough and keeps us off the wire.
SECRET_TTL_SECONDS = 24 * 3600

_PERIOD = 30
_DIGITS = 6


class SpotifyTOTPError(Exception):
    """TOTP secret fetch / decode failed; caller should skip Spotify."""


def _hotp_sha1(secret: bytes, timestamp_ms: int) -> str:
    """RFC 6238 HMAC-SHA1 6-digit TOTP."""
    counter = math.floor(timestamp_ms / 1000 / _PERIOD)
    counter_bytes = counter.to_bytes(8, byteorder="big")
    digest = hmac.new(secret, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        (digest[offset] & 0x7F) << 24
        | (digest[offset + 1] & 0xFF) << 16
        | (digest[offset + 2] & 0xFF) << 8
        | (digest[offset + 3] & 0xFF)
    )
    return str(binary % (10**_DIGITS)).zfill(_DIGITS)


def _decode_secret(ascii_codes: list[int]) -> bytes:
    """Reverse the XOR cipher used by ``xyloflake/spot-secrets-go``."""
    transformed = [val ^ ((i % 33) + 9) for i, val in enumerate(ascii_codes)]
    return "".join(str(num) for num in transformed).encode("utf-8")


class SpotifyTOTP:
    """Process-wide TOTP code generator with cached upstream secret."""

    _instance: "SpotifyTOTP | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._cache: tuple[bytes, int, float] | None = None
        self._cache_lock = threading.Lock()

    @classmethod
    def singleton(cls) -> "SpotifyTOTP":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    def generate(self, timestamp_ms: int) -> tuple[str, int]:
        """Return ``(otp_6digit, secret_version)`` for the given epoch ms."""
        secret, version = self._get_secret()
        return _hotp_sha1(secret, timestamp_ms), version

    def _get_secret(self) -> tuple[bytes, int]:
        with self._cache_lock:
            if self._cache is not None:
                secret, version, fetched_at = self._cache
                if time.time() - fetched_at < SECRET_TTL_SECONDS:
                    return secret, version
            secret, version = self._fetch_latest()
            self._cache = (secret, version, time.time())
            return secret, version

    @staticmethod
    def _fetch_latest() -> tuple[bytes, int]:
        try:
            r = requests.get(SECRET_CIPHER_DICT_URL, timeout=SECRET_FETCH_TIMEOUT)
        except requests.RequestException as e:
            raise SpotifyTOTPError(f"secret fetch failed: {e}") from e
        if r.status_code != 200:
            raise SpotifyTOTPError(f"secret fetch HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise SpotifyTOTPError(f"secret JSON decode failed: {e}") from e
        if not isinstance(data, dict) or not data:
            raise SpotifyTOTPError("secret dict is empty")
        try:
            latest_key = max(data.keys(), key=int)
            version = int(latest_key)
            secret = _decode_secret(data[latest_key])
        except (ValueError, TypeError) as e:
            raise SpotifyTOTPError(f"secret dict malformed: {e}") from e
        if not secret:
            raise SpotifyTOTPError("decoded secret is empty")
        return secret, version

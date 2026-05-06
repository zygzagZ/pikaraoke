"""Unit tests for pikaraoke.lib.lyrics_spotify_totp."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from pikaraoke.lib.lyrics_spotify_totp import (
    SpotifyTOTP,
    SpotifyTOTPError,
    _decode_secret,
    _hotp_sha1,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    SpotifyTOTP.reset_for_tests()
    yield
    SpotifyTOTP.reset_for_tests()


class TestHotpSha1:
    def test_rfc6238_vector(self):
        """RFC 6238 Appendix B: SHA-1, K='12345678901234567890', T=59 → 287082.

        Standard 8-digit reference is 94287082; we trim to 6 digits, which
        matches Spotify's expected wire format and verifies our HMAC + DT
        offset arithmetic against the published vector.
        """
        assert _hotp_sha1(b"12345678901234567890", 59_000) == "287082"

    def test_period_boundary(self):
        # Same 30s window → identical code; one second past → different code.
        a = _hotp_sha1(b"12345678901234567890", 29_999)
        b = _hotp_sha1(b"12345678901234567890", 0)
        assert a == b
        c = _hotp_sha1(b"12345678901234567890", 30_000)
        assert c != b


class TestDecodeSecret:
    def test_xor_cipher_roundtrip(self):
        # Live xyloflake/spot-secrets-go v61 cipher (snapshot 2026-01-26).
        codes = [
            44,
            55,
            47,
            42,
            70,
            40,
            34,
            114,
            76,
            74,
            50,
            111,
            120,
            97,
            75,
            76,
            94,
            102,
            43,
            69,
            49,
            120,
            118,
            80,
            64,
            78,
        ]
        # transformed[i] = codes[i] XOR ((i % 33) + 9), then digits joined.
        secret = _decode_secret(codes)
        assert secret == b"376136387538459893883312310911992847112448894410210511297108"


class TestSpotifyTOTPSingleton:
    def _patch_secret_response(self, payload):
        resp = MagicMock(status_code=200)
        resp.json.return_value = payload
        return patch(
            "pikaraoke.lib.lyrics_spotify_totp.requests.get",
            return_value=resp,
        )

    def test_picks_highest_version_key(self):
        # Mixed key order, _fetch_latest must pick numeric max ("61").
        payload = {
            "59": [123, 105, 79, 70, 110, 59, 52, 125],
            "61": [44, 55, 47, 42],
            "60": [79, 109, 69, 123],
        }
        with self._patch_secret_response(payload):
            code, version = SpotifyTOTP.singleton().generate(0)
        assert version == 61
        assert len(code) == 6 and code.isdigit()

    def test_secret_cache_hit_skips_network(self):
        payload = {"61": [44, 55, 47, 42]}
        with self._patch_secret_response(payload) as mock_get:
            totp = SpotifyTOTP.singleton()
            totp.generate(0)
            totp.generate(60_000)
            assert mock_get.call_count == 1

    def test_fetch_failure_raises(self):
        with patch(
            "pikaraoke.lib.lyrics_spotify_totp.requests.get",
            side_effect=requests.ConnectionError("boom"),
        ):
            with pytest.raises(SpotifyTOTPError):
                SpotifyTOTP.singleton().generate(0)

    def test_http_non_200_raises(self):
        with patch(
            "pikaraoke.lib.lyrics_spotify_totp.requests.get",
            return_value=MagicMock(status_code=502),
        ):
            with pytest.raises(SpotifyTOTPError):
                SpotifyTOTP.singleton().generate(0)

    def test_empty_dict_raises(self):
        with self._patch_secret_response({}):
            with pytest.raises(SpotifyTOTPError):
                SpotifyTOTP.singleton().generate(0)

    def test_malformed_dict_raises(self):
        # Non-numeric key fails int() conversion in max(...).
        with self._patch_secret_response({"abc": [1, 2, 3]}):
            with pytest.raises(SpotifyTOTPError):
                SpotifyTOTP.singleton().generate(0)

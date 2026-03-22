"""
test_push_utils.py — Tests para push_utils.py.

Se testean:
  - Funciones criptográficas puras (sin red): _b64url_encode/_decode, _make_vapid_jwt
  - Comportamiento de send_push_to_all con subscripciones mockeadas
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ec

from push_utils import (
    _b64url_decode,
    _b64url_encode,
    _make_vapid_jwt,
    send_push_to_all,
)


# ── base64url ─────────────────────────────────────────────────────────────────

class TestBase64url:
    def test_encode_and_decode_roundtrip(self):
        data = b"hello, world! \x00\xff"
        encoded = _b64url_encode(data)
        decoded = _b64url_decode(encoded)
        assert decoded == data

    def test_encode_has_no_padding(self):
        encoded = _b64url_encode(b"test")
        assert "=" not in encoded

    def test_encode_uses_urlsafe_alphabet(self):
        # urlsafe usa - y _ en lugar de + y /
        # bytes que generan + o / en base64 normal
        data = bytes(range(256))
        encoded = _b64url_encode(data)
        assert "+" not in encoded
        assert "/" not in encoded

    def test_decode_handles_missing_padding(self):
        # base64url sin padding debe decodificarse igualmente
        original = b"market-radar"
        encoded = _b64url_encode(original)
        # Eliminar padding si lo hubiera (no debería haberlo)
        encoded_no_pad = encoded.rstrip("=")
        assert _b64url_decode(encoded_no_pad) == original

    def test_empty_bytes(self):
        assert _b64url_encode(b"") == ""
        assert _b64url_decode("") == b""


# ── _make_vapid_jwt ───────────────────────────────────────────────────────────

class TestMakeVapidJwt:
    @pytest.fixture(autouse=True)
    def _private_key(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())

    def test_jwt_has_three_parts(self):
        jwt = _make_vapid_jwt("https://fcm.googleapis.com", self.private_key)
        parts = jwt.split(".")
        assert len(parts) == 3

    def test_jwt_header_is_correct(self):
        jwt = _make_vapid_jwt("https://push.example.com", self.private_key)
        header_b64 = jwt.split(".")[0]
        header = json.loads(_b64url_decode(header_b64))
        assert header["typ"] == "JWT"
        assert header["alg"] == "ES256"

    def test_jwt_payload_contains_audience(self):
        audience = "https://push.example.com"
        jwt = _make_vapid_jwt(audience, self.private_key)
        payload_b64 = jwt.split(".")[1]
        payload = json.loads(_b64url_decode(payload_b64))
        assert payload["aud"] == audience

    def test_jwt_payload_has_exp_in_future(self):
        import time
        jwt = _make_vapid_jwt("https://push.example.com", self.private_key)
        payload_b64 = jwt.split(".")[1]
        payload = json.loads(_b64url_decode(payload_b64))
        assert payload["exp"] > int(time.time())

    def test_jwt_signature_is_64_bytes(self):
        jwt = _make_vapid_jwt("https://push.example.com", self.private_key)
        sig_b64 = jwt.split(".")[2]
        sig_bytes = _b64url_decode(sig_b64)
        assert len(sig_bytes) == 64  # r (32) + s (32)


# ── send_push_to_all ──────────────────────────────────────────────────────────

class TestSendPushToAll:
    """
    Verifica el comportamiento de send_push_to_all sin hacer peticiones HTTP reales.
    Se mockean get_all_push_subscriptions y send_push_notification.
    """

    def _make_subs(self, n):
        return [(f"https://fcm.example.com/sub{i}", f"p256dh_{i}", f"auth_{i}")
                for i in range(n)]

    def test_returns_zero_when_no_subscriptions(self, tmp_db):
        with patch("push_utils.get_all_push_subscriptions", return_value=[]):
            result = send_push_to_all("título", "cuerpo")
        assert result == 0

    def test_counts_successful_sends(self, tmp_db):
        subs = self._make_subs(3)
        with patch("push_utils.get_all_push_subscriptions", return_value=subs), \
             patch("push_utils.send_push_notification", return_value=True):
            result = send_push_to_all("título", "cuerpo")
        assert result == 3

    def test_expired_subscription_deleted_on_false(self, tmp_db):
        subs = self._make_subs(1)
        deleted = []

        def fake_delete(ep):
            deleted.append(ep)

        with patch("push_utils.get_all_push_subscriptions", return_value=subs), \
             patch("push_utils.send_push_notification", return_value=False), \
             patch("push_utils.delete_push_subscription", side_effect=fake_delete):
            result = send_push_to_all("título", "cuerpo")

        assert result == 0
        assert len(deleted) == 1
        assert deleted[0] == subs[0][0]

    def test_temporary_error_does_not_delete_subscription(self, tmp_db):
        subs = self._make_subs(2)
        deleted = []

        def fake_delete(ep):
            deleted.append(ep)

        with patch("push_utils.get_all_push_subscriptions", return_value=subs), \
             patch("push_utils.send_push_notification", return_value=None), \
             patch("push_utils.delete_push_subscription", side_effect=fake_delete):
            result = send_push_to_all("título", "cuerpo")

        assert result == 0
        assert deleted == []  # ninguna eliminada

    def test_mixed_results(self, tmp_db):
        """2 éxito, 1 expirada (410), 1 error temporal."""
        subs = self._make_subs(4)
        results = [True, True, False, None]
        deleted = []

        def fake_send(ep, *args, **kwargs):
            idx = int(ep.split("sub")[1])
            return results[idx]

        def fake_delete(ep):
            deleted.append(ep)

        with patch("push_utils.get_all_push_subscriptions", return_value=subs), \
             patch("push_utils.send_push_notification", side_effect=fake_send), \
             patch("push_utils.delete_push_subscription", side_effect=fake_delete):
            result = send_push_to_all("título", "cuerpo")

        assert result == 2
        assert len(deleted) == 1  # solo la 410

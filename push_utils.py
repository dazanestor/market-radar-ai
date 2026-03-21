"""
push_utils.py — Web Push VAPID (RFC 8292) + payload encryption (RFC 8291).
Sin dependencias externas: usa solo 'cryptography' (ya en requirements.txt)
y 'requests' (ya en requirements.txt).
"""
import base64
import json
import logging
import os
import struct
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from database import (
    delete_push_subscription,
    get_all_push_subscriptions,
    get_setting,
    set_setting,
)

logger = logging.getLogger("push_utils")

VAPID_SUBJECT = "mailto:admin@localhost"

# ── Utilidades base64url ───────────────────────────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)

# ── Gestión de claves VAPID ────────────────────────────────────────────────────

def get_or_create_vapid_keys() -> tuple:
    """
    Devuelve (private_pem, public_b64url).
    Si no existen en BD, las genera y las persiste en settings.
    """
    priv_pem = get_setting("vapid_private_pem")
    pub_b64  = get_setting("vapid_public_b64")
    if priv_pem and pub_b64:
        return priv_pem, pub_b64

    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = _b64url_encode(pub_bytes)

    set_setting("vapid_private_pem", priv_pem)
    set_setting("vapid_public_b64", pub_b64)
    logger.info("Nuevas claves VAPID generadas y guardadas en BD.")
    return priv_pem, pub_b64


def _load_private_key(pem: str) -> ec.EllipticCurvePrivateKey:
    return serialization.load_pem_private_key(
        pem.encode(), password=None, backend=default_backend()
    )

# ── JWT VAPID (RFC 8292) ───────────────────────────────────────────────────────

def _make_vapid_jwt(audience: str, private_key: ec.EllipticCurvePrivateKey) -> str:
    header  = _b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    payload = _b64url_encode(json.dumps({
        "aud": audience,
        "exp": int(time.time()) + 43200,
        "sub": VAPID_SUBJECT,
    }).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{payload}.{_b64url_encode(raw_sig)}"

# ── Cifrado de payload RFC 8291 (aesgcm) ──────────────────────────────────────

def _encrypt_payload(plaintext: bytes, sub_p256dh: str, sub_auth: str):
    """
    Cifra el payload siguiendo RFC 8291 con content-encoding 'aesgcm'.
    Devuelve (ciphertext, salt, server_pub_bytes).
    """
    receiver_pub_bytes = _b64url_decode(sub_p256dh)
    auth_secret        = _b64url_decode(sub_auth)

    receiver_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), receiver_pub_bytes
    )

    server_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    server_pub_bytes = server_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    shared_secret = server_key.exchange(ec.ECDH(), receiver_pub)
    salt = os.urandom(16)

    prk = HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=auth_secret,
        info=b"Content-Encoding: auth\x00",
        backend=default_backend(),
    ).derive(shared_secret)

    context = (
        b"P-256\x00"
        + struct.pack(">H", len(receiver_pub_bytes)) + receiver_pub_bytes
        + struct.pack(">H", len(server_pub_bytes))   + server_pub_bytes
    )

    cek = HKDF(
        algorithm=hashes.SHA256(), length=16,
        salt=salt,
        info=b"Content-Encoding: aesgcm\x00" + context,
        backend=default_backend(),
    ).derive(prk)

    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12,
        salt=salt,
        info=b"Content-Encoding: nonce\x00" + context,
        backend=default_backend(),
    ).derive(prk)

    padded = b"\x00\x00" + plaintext
    ciphertext = AESGCM(cek).encrypt(nonce, padded, None)
    return ciphertext, salt, server_pub_bytes

# ── Envío de notificación ──────────────────────────────────────────────────────

def send_push_notification(endpoint: str, p256dh: str, auth: str,
                           title: str, body: str, url: str = "/alertas"):
    """
    Envía una Web Push notification a una suscripción concreta.
    Devuelve:
      True  — servidor de push aceptó la entrega (2xx).
      False — suscripción expirada/inválida (410 Gone); debe eliminarse.
      None  — error temporal (red, timeout, 5xx); la suscripción sigue válida.
    """
    try:
        priv_pem, pub_b64 = get_or_create_vapid_keys()
        private_key = _load_private_key(priv_pem)

        parsed   = urlparse(endpoint)
        audience = f"{parsed.scheme}://{parsed.netloc}"

        jwt_token = _make_vapid_jwt(audience, private_key)

        payload_json = json.dumps({
            "title": title,
            "body":  body,
            "url":   url,
            "icon":  "/icon-192.png",
        }).encode("utf-8")

        ciphertext, salt, server_pub = _encrypt_payload(payload_json, p256dh, auth)

        headers = {
            "Authorization":    f"vapid t={jwt_token},k={pub_b64}",
            "Content-Type":     "application/octet-stream",
            "Content-Encoding": "aesgcm",
            "Encryption":       f"salt={_b64url_encode(salt)}",
            "Crypto-Key":       f"dh={_b64url_encode(server_pub)};p256ecdsa={pub_b64}",
            "TTL":              "86400",
        }

        resp = requests.post(endpoint, data=ciphertext, headers=headers, timeout=10)
        if resp.status_code in (200, 201, 202):
            return True
        if resp.status_code == 410:
            logger.warning("Suscripción expirada (410 Gone): %s", endpoint[:60])
            return False
        logger.warning("Push rechazado: %s %s", resp.status_code, resp.text[:100])
        return None
    except Exception as e:
        logger.error("Error enviando push: %s", e)
        return None


def send_push_to_all(title: str, body: str, url: str = "/alertas") -> int:
    """
    Envía la notificación a todas las suscripciones activas en paralelo.
    Elimina solo las suscripciones confirmadas como expiradas (410 Gone).
    Los errores temporales (red, timeout, 5xx) conservan la suscripción.
    Devuelve el número de envíos exitosos.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    subs = get_all_push_subscriptions()
    if not subs:
        return 0

    def _send(sub):
        endpoint, p256dh, auth = sub
        return endpoint, send_push_notification(endpoint, p256dh, auth, title, body, url)

    ok = 0
    with ThreadPoolExecutor(max_workers=min(len(subs), 8)) as pool:
        futures = [pool.submit(_send, s) for s in subs]
        for fut in as_completed(futures):
            try:
                endpoint, result = fut.result()
                if result is True:
                    ok += 1
                elif result is False:
                    # Solo eliminar en 410 Gone (suscripción confirmada como inválida)
                    try:
                        delete_push_subscription(endpoint)
                    except Exception:
                        pass
                # result is None → error temporal, conservar suscripción
            except Exception:
                pass
    return ok

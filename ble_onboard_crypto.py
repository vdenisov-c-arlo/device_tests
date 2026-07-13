"""BLE Onboarding Crypto — ECDH key exchange + AES-256-CBC encryption."""

import os
import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from ble_onboard_constants import (
    COMMISSIONER_KEY_SIZE,
    PBKDF2_ITERATIONS,
    PBKDF2_OUTPUT_SIZE,
)


def generate_keypair():
    """Generate ephemeral EC P-256 key pair.

    Returns:
        (private_key, public_key_bytes) where public_key_bytes is 65 bytes uncompressed.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    assert len(public_key_bytes) == COMMISSIONER_KEY_SIZE
    return private_key, public_key_bytes


def compute_shared_secret(our_private_key, device_public_key_bytes):
    """Compute ECDH shared secret and return base64-encoded password.

    Args:
        our_private_key: Our EC private key object.
        device_public_key_bytes: Device's 65-byte uncompressed public key.

    Returns:
        Base64-encoded shared secret string (used as PBKDF2 password).
    """
    device_public_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), device_public_key_bytes
    )
    shared_secret = our_private_key.exchange(ec.ECDH(), device_public_key)
    return base64.b64encode(shared_secret).decode("ascii")


def encrypt_value(shared_secret_b64, plaintext):
    """Encrypt a value using the BLE onboarding crypto protocol.

    Format: "Salted__" + salt[8] + AES-256-CBC(plaintext)
    Key derivation: PBKDF2-HMAC-SHA256(password=shared_secret_b64, salt, 10000 iters, 48 bytes)
    First 32 bytes = AES key, last 16 bytes = IV.

    Args:
        shared_secret_b64: Base64-encoded ECDH shared secret string.
        plaintext: bytes to encrypt.

    Returns:
        Encrypted bytes in "Salted__" format.
    """
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")

    salt = os.urandom(8)

    dk = hashlib.pbkdf2_hmac(
        "sha256",
        shared_secret_b64.encode("ascii"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=PBKDF2_OUTPUT_SIZE,
    )
    aes_key = dk[:32]
    iv = dk[32:48]

    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return b"Salted__" + salt + ciphertext


def extract_pubkey_from_birth_cert(birth_cert_json):
    """Extract EC P-256 public key from device birth certificate JSON.

    The birth cert contains a "privateKey" field (base64-encoded DER EC private key).
    We derive the public key from it.

    Args:
        birth_cert_json: JSON string or dict of birth certificate.

    Returns:
        65-byte uncompressed public key bytes.
    """
    if isinstance(birth_cert_json, str):
        birth_cert_json = json.loads(birth_cert_json)

    private_key_b64 = birth_cert_json["privateKey"]
    private_key_der = base64.b64decode(private_key_b64)

    private_key = serialization.load_der_private_key(private_key_der, password=None)

    public_key_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    assert len(public_key_bytes) == COMMISSIONER_KEY_SIZE
    return public_key_bytes


def extract_cert_id_from_birth_cert(birth_cert_json):
    """Extract certificate ID from birth certificate JSON.

    Args:
        birth_cert_json: JSON string or dict of birth certificate.

    Returns:
        Certificate ID as hex string (32 chars) or bytes (16 bytes).
    """
    if isinstance(birth_cert_json, str):
        birth_cert_json = json.loads(birth_cert_json)

    cert_id = birth_cert_json["certId"]
    return cert_id

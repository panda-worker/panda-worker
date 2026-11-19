# -*- coding: utf-8 -*-
"""
panda_worker/release_verify.py — проверка целостности и подписи релизов (v2.5.0).

FIX-2026-11-19 (P0-3 / P0-4):
* раньше hot-update проверялся только по SHA-256, который приходил с того же сервера
  (по HTTP), а пакет движков — вообще никак. Подмена трафика = выполнение чужого кода;
* при отсутствии `cryptography` «подпись» проверялась как HMAC с ПУБЛИЧНЫМ ключом
  (подделать мог кто угодно).

Теперь: обязательные SHA-256 + Ed25519-подпись над строкой sha256-hex; ключ вшит в
сборку (worker_config.RELEASE_PUBKEY_HEX). Нет ключа / нет подписи / нет
`cryptography` -> отказ (fail-closed). Никакого HMAC-фолбэка.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

import worker_config as wcfg

log = logging.getLogger("panda_worker")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(524288), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def verify_release_file(path: Path, sha256_hex: str, sig_b64: str, what: str = "пакет") -> bool:
    """True only when SHA-256 matches AND the Ed25519 signature over the sha hex is valid."""
    sha256_hex = (sha256_hex or "").strip().lower()
    if len(sha256_hex) != 64:
        log.error("%s отклонён: сервер не передал SHA-256.", what)
        return False
    actual = sha256_of(path)
    if actual != sha256_hex:
        log.error("%s отклонён: SHA-256 не совпал (%s != %s).", what, actual[:12], sha256_hex[:12])
        return False
    return verify_signature(sha256_hex, sig_b64, what)


def verify_signature(sha256_hex: str, sig_b64: str, what: str = "пакет") -> bool:
    pubkey_hex = (wcfg.RELEASE_PUBKEY_HEX or "").strip()
    if len(pubkey_hex) != 64:
        log.error("%s отклонён: в сборке нет ключа проверки подписи (RELEASE_PUBKEY_HEX).", what)
        return False
    if not sig_b64:
        log.error("%s отклонён: сервер не передал подпись.", what)
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # type: ignore
    except ImportError:
        log.error("%s отклонён: в сборке нет модуля cryptography (проверка Ed25519 невозможна).", what)
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(base64.b64decode(sig_b64, validate=True), sha256_hex.strip().lower().encode("ascii"))
        return True
    except Exception as exc:  # noqa: BLE001 - InvalidSignature, binascii.Error, ValueError
        log.error("%s отклонён: подпись не прошла проверку (%s).", what, type(exc).__name__)
        return False

# -*- coding: utf-8 -*-
"""
Panda Worker Protocol v1.0 — Strict Job Schema, Per-Node Credentials & Integrity Helpers.

Security invariants:
1. Never accepts or transmits shell commands, arbitrary filesystem paths, or Telegram BOT_TOKEN.
2. Every job parameter is validated against a strict allowlist of types, enums, and numeric bounds.
3. Each Worker node authenticates via its own revocable token (`pwk_<worker_id>.<secret>`);
   the server stores only PBKDF2-HMAC-SHA256(secret, salt) and never a shared cluster secret.
4. Every media transfer (input and output) is verified by exact byte size + SHA-256 digest.
5. Multi-file photo bundles enforce strict filename allowlists and decompression caps.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Optional, Tuple

PROTOCOL_VERSION = "1.0"

TASK_VIDEO_ENHANCE = "video_enhance"
TASK_PHOTO_ENHANCE = "photo_enhance"
TASK_VIDEO_TOOL = "video_tool"
TASK_PHOTO_TOOL = "photo_tool"
TASK_SQUEEZE_MEDIA = "squeeze_media"

ALLOWED_TASK_TYPES = frozenset(
    {
        TASK_VIDEO_ENHANCE,
        TASK_PHOTO_ENHANCE,
        TASK_VIDEO_TOOL,
        TASK_PHOTO_TOOL,
        TASK_SQUEEZE_MEDIA,
    }
)

ALLOWED_POOLS = frozenset({"trusted", "volunteer"})
ALLOWED_PRIVACY_SCOPES = frozenset({"trusted_only", "volunteer_consented"})

ALLOWED_VIDEO_MODELS = frozenset({"auto", "turbo", "real", "anime"})
ALLOWED_PHOTO_MODELS = frozenset({"auto", "real", "anime", "anime_pro", "conserv", "photo_hq", "photo_natural"})
ALLOWED_SHARPNESS = frozenset({"auto", "soft", "hd", "ultra", "raw", "balanced", "vivid"})
ALLOWED_MASTERING = frozenset({"auto", "hdr", "cinema", "raw"})
ALLOWED_VIDEO_EDGES = frozenset({1280, 1920, 2560})
ALLOWED_LANGS = frozenset({"kk", "ru", "en"})
ALLOWED_VIDEO_TOOLS = frozenset({"video_9_16", "video_gif", "video_circle"})
ALLOWED_PHOTO_TOOLS = frozenset({"cutout_bg", "blur_bg", "colorize_bw"})
ALLOWED_SQUEEZE_KINDS = frozenset({"photo", "video"})

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{3,48}$")
_SAFE_BUNDLE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

MAX_BUNDLE_UNPACKED_BYTES = 180 * 1024 * 1024
MAX_META_JSON_BYTES = 64 * 1024

# Pinned Ed25519 public key (base64 raw 32-byte key) for signed Worker self-updates.
# Intentionally empty until owner explicitly generates and approves a production signing key.
PINNED_UPDATE_ED25519_PUBKEY_B64: str = ""


class ProtocolValidationError(ValueError):
    """Raised when a job payload, credential, or bundle violates strict protocol rules."""


def is_safe_identifier(value: str) -> bool:
    return bool(isinstance(value, str) and _SAFE_ID_RE.fullmatch(value))


def generate_worker_credentials(worker_id: Optional[str] = None) -> Tuple[str, str, str, str]:
    """
    Generate unique revocable credentials for one Worker PC.
    Returns (worker_id, plaintext_token, salt_hex, hash_hex).
    `plaintext_token` is shown ONCE for Worker `.env`; server stores only `(salt_hex, hash_hex)`.
    """
    wid = worker_id or f"node_{secrets.token_hex(5)}"
    if not is_safe_identifier(wid):
        raise ProtocolValidationError(f"Invalid worker_id: {wid!r}")
    secret = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 200_000)
    token = f"pwk_{wid}.{secret}"
    return wid, token, salt.hex(), digest.hex()


def parse_worker_bearer(header_value: str) -> Tuple[str, str]:
    """Parse `Bearer pwk_<worker_id>.<secret>` into `(worker_id, secret)`."""
    raw = (header_value or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw.startswith("pwk_") or "." not in raw:
        raise ProtocolValidationError("Malformed worker token")
    prefix, secret = raw.split(".", 1)
    wid = prefix[4:]
    if not is_safe_identifier(wid) or len(secret) < 20 or len(secret) > 128:
        raise ProtocolValidationError("Invalid worker token structure")
    return wid, secret


def verify_worker_secret(secret: str, salt_hex: str, expected_hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hash_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 200_000)
    return hmac.compare_digest(actual, expected)


def _require_bool(val: Any, name: str) -> bool:
    if isinstance(val, bool):
        return val
    if val in (0, 1):
        return bool(val)
    raise ProtocolValidationError(f"Parameter {name!r} must be bool")


def _require_int(val: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ProtocolValidationError(f"Parameter {name!r} must be int")
    if val < lo or val > hi:
        raise ProtocolValidationError(f"Parameter {name!r}={val} out of range [{lo}, {hi}]")
    return val


def _require_enum(val: Any, name: str, allowed: Iterable[Any]) -> Any:
    allowed_set = set(allowed)
    if val not in allowed_set:
        raise ProtocolValidationError(f"Parameter {name!r}={val!r} not in allowlist")
    return val


def validate_job_payload(task_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and canonicalize job parameters against the strict task schema.
    Rejects any unexpected key, path-like string, token, or out-of-bounds value.
    """
    if task_type not in ALLOWED_TASK_TYPES:
        raise ProtocolValidationError(f"Unsupported task_type: {task_type!r}")
    if not isinstance(params, dict):
        raise ProtocolValidationError("params must be a JSON object")

    if task_type == TASK_VIDEO_ENHANCE:
        allowed_keys = {
            "video_model",
            "enable_60fps",
            "color_enhance",
            "sharpness_mode",
            "mastering_mode",
            "max_seconds",
            "target_long_edge",
            "is_paid_tier",
            "smart_hevc",
            "lang",
        }
        extra = set(params.keys()) - allowed_keys
        if extra:
            raise ProtocolValidationError(f"Unexpected keys in video_enhance: {sorted(extra)}")
        return {
            "video_model": _require_enum(params.get("video_model", "auto"), "video_model", ALLOWED_VIDEO_MODELS),
            "enable_60fps": _require_bool(params.get("enable_60fps", True), "enable_60fps"),
            "color_enhance": _require_bool(params.get("color_enhance", True), "color_enhance"),
            "sharpness_mode": _require_enum(
                params.get("sharpness_mode", "auto"), "sharpness_mode", ALLOWED_SHARPNESS
            ),
            "mastering_mode": _require_enum(
                params.get("mastering_mode", "auto"), "mastering_mode", ALLOWED_MASTERING
            ),
            "max_seconds": _require_int(params.get("max_seconds", 15), "max_seconds", 1, 180),
            "target_long_edge": _require_enum(
                params.get("target_long_edge", 1280), "target_long_edge", ALLOWED_VIDEO_EDGES
            ),
            "is_paid_tier": _require_bool(params.get("is_paid_tier", False), "is_paid_tier"),
            "smart_hevc": _require_bool(params.get("smart_hevc", True), "smart_hevc"),
            "lang": _require_enum(params.get("lang", "ru"), "lang", ALLOWED_LANGS),
        }

    if task_type == TASK_PHOTO_ENHANCE:
        allowed_keys = {
            "photo_model",
            "sharpness_mode",
            "mastering_mode",
            "color_enhance",
            "anti_blur",
            "scale_factor",
            "max_target_px",
            "build_split",
        }
        extra = set(params.keys()) - allowed_keys
        if extra:
            raise ProtocolValidationError(f"Unexpected keys in photo_enhance: {sorted(extra)}")
        return {
            "photo_model": _require_enum(params.get("photo_model", "auto"), "photo_model", ALLOWED_PHOTO_MODELS),
            "sharpness_mode": _require_enum(
                params.get("sharpness_mode", "auto"), "sharpness_mode", ALLOWED_SHARPNESS
            ),
            "mastering_mode": _require_enum(
                params.get("mastering_mode", "auto"), "mastering_mode", ALLOWED_MASTERING
            ),
            "color_enhance": _require_bool(params.get("color_enhance", True), "color_enhance"),
            "anti_blur": _require_bool(params.get("anti_blur", True), "anti_blur"),
            "scale_factor": _require_enum(params.get("scale_factor", 4), "scale_factor", {2, 4}),
            "max_target_px": _require_int(params.get("max_target_px", 3840), "max_target_px", 640, 4096),
            "build_split": _require_bool(params.get("build_split", True), "build_split"),
        }

    if task_type == TASK_VIDEO_TOOL:
        if set(params.keys()) - {"tool"}:
            raise ProtocolValidationError("Unexpected keys in video_tool")
        return {"tool": _require_enum(params.get("tool"), "tool", ALLOWED_VIDEO_TOOLS)}

    if task_type == TASK_PHOTO_TOOL:
        if set(params.keys()) - {"tool"}:
            raise ProtocolValidationError("Unexpected keys in photo_tool")
        return {"tool": _require_enum(params.get("tool"), "tool", ALLOWED_PHOTO_TOOLS)}

    if task_type == TASK_SQUEEZE_MEDIA:
        if set(params.keys()) - {"kind"}:
            raise ProtocolValidationError("Unexpected keys in squeeze_media")
        return {"kind": _require_enum(params.get("kind"), "kind", ALLOWED_SQUEEZE_KINDS)}

    raise ProtocolValidationError(f"Unhandled task_type: {task_type!r}")


def required_capability_for_job(task_type: str, params: Dict[str, Any]) -> str:
    """Return capability tag a Worker must advertise to claim this job."""
    if task_type == TASK_VIDEO_ENHANCE:
        if params.get("enable_60fps") and params.get("is_paid_tier"):
            return "video_esrgan_rife"
        return "video_esrgan"
    if task_type == TASK_PHOTO_ENHANCE:
        return "photo_esrgan"
    if task_type == TASK_VIDEO_TOOL:
        return "ffmpeg"
    if task_type == TASK_PHOTO_TOOL:
        return "photo_tools"
    if task_type == TASK_SQUEEZE_MEDIA:
        return "ffmpeg"
    return "ffmpeg"


def sha256_file(path: Path, chunk_size: int = 262_144) -> Tuple[str, int]:
    """Compute `(sha256_hex, size_bytes)` of a file without loading it into memory."""
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def stream_to_file_verified(
    stream: BinaryIO,
    dest_path: Path,
    max_bytes: int,
    expected_bytes: Optional[int] = None,
    expected_sha256: Optional[str] = None,
    chunk_size: int = 262_144,
) -> Tuple[str, int]:
    """
    Stream exact `expected_bytes` (or until EOF when `expected_bytes is None`) to `dest_path`
    atomically via `.part`, enforcing `max_bytes` and optional `expected_sha256`.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_suffix(dest_path.suffix + f".{os.getpid()}.part")
    h = hashlib.sha256()
    total = 0
    try:
        with open(part_path, "wb") as out:
            remaining = expected_bytes
            while True:
                to_read = chunk_size if remaining is None else min(chunk_size, remaining)
                if remaining is not None and to_read <= 0:
                    break
                chunk = stream.read(to_read)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ProtocolValidationError(f"Stream exceeds limit of {max_bytes} bytes")
                h.update(chunk)
                out.write(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
        if expected_bytes is not None and total != expected_bytes:
            raise ProtocolValidationError(
                f"Incomplete stream: got {total} bytes, expected {expected_bytes}"
            )
        digest = h.hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.strip().lower():
            raise ProtocolValidationError(
                f"SHA-256 mismatch: got {digest[:16]}..., expected {expected_sha256[:16]}..."
            )
        part_path.replace(dest_path)
        return digest, total
    except Exception:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def pack_result_bundle(
    out_zip: Path,
    meta: Dict[str, Any],
    files: Dict[str, Path],
) -> Tuple[str, int]:
    """
    Pack structured Worker outputs (`meta.json` + named output files) into a safe ZIP bundle.
    Returns `(sha256_hex, size_bytes)`.
    """
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        if len(meta_bytes) > MAX_META_JSON_BYTES:
            raise ProtocolValidationError("Result metadata exceeds 64 KB")
        zf.writestr("meta.json", meta_bytes)
        for arc_name, src_path in files.items():
            if not _SAFE_BUNDLE_NAME_RE.fullmatch(arc_name) or arc_name == "meta.json":
                raise ProtocolValidationError(f"Unsafe archive member name: {arc_name!r}")
            zf.write(src_path, arcname=arc_name)
    return sha256_file(out_zip)


def unpack_result_bundle(bundle_zip: Path, dest_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Path]]:
    """
    Safely unpack a Worker ZIP bundle into `dest_dir`, defending against path traversal,
    symlinks, unexpected names, and decompression bombs.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = dest_dir.resolve()
    extracted: Dict[str, Path] = {}
    with zipfile.ZipFile(bundle_zip, "r") as zf:
        infos = zf.infolist()
        if len(infos) > 8:
            raise ProtocolValidationError("Too many files in worker result bundle")
        total_unpacked = sum(max(0, info.file_size) for info in infos)
        if total_unpacked > MAX_BUNDLE_UNPACKED_BYTES:
            raise ProtocolValidationError("Worker result bundle unpacked size exceeds limit")
        names = {info.filename for info in infos}
        if "meta.json" not in names:
            raise ProtocolValidationError("Worker result bundle missing meta.json")

        for info in infos:
            name = info.filename
            if (
                not _SAFE_BUNDLE_NAME_RE.fullmatch(name)
                or ".." in name
                or "/" in name
                or "\\" in name
                or ":" in name
            ):
                raise ProtocolValidationError(f"Unsafe archive entry: {name!r}")
            # Reject Unix symlinks encoded in external_attr
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ProtocolValidationError("Symlinks forbidden in worker result bundle")

        meta_info = zf.getinfo("meta.json")
        if meta_info.file_size > MAX_META_JSON_BYTES:
            raise ProtocolValidationError("meta.json too large")
        meta = json.loads(zf.read("meta.json").decode("utf-8"))
        if not isinstance(meta, dict):
            raise ProtocolValidationError("meta.json must be an object")

        for info in infos:
            if info.filename == "meta.json":
                continue
            target = (dest_dir / info.filename).resolve()
            if target.parent != resolved_root:
                raise ProtocolValidationError("Bundle member escaped destination directory")
            written = 0
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                while True:
                    buf = src.read(262_144)
                    if not buf:
                        break
                    written += len(buf)
                    if written > MAX_BUNDLE_UNPACKED_BYTES:
                        raise ProtocolValidationError("Bundle member exceeded unpacked limit")
                    dst.write(buf)
            extracted[info.filename] = target

    return meta, extracted


def verify_signed_release(
    payload_bytes: bytes,
    signature_b64: str,
    pubkey_b64: str = PINNED_UPDATE_ED25519_PUBKEY_B64,
) -> bool:
    """
    Verify an Ed25519 signature over a Worker update bundle using a pinned public key.
    Fails closed (`False`) when no production public key has been pinned or cryptography is absent.
    """
    if not pubkey_b64 or not signature_b64:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        sig = base64.b64decode(signature_b64)
        pub.verify(sig, payload_bytes)
        return True
    except Exception:
        return False

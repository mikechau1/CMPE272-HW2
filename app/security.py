"""Webhook signature verification.

GitHub signs each delivery with ``X-Hub-Signature-256: sha256=<hex digest>``,
an HMAC-SHA256 of the **raw** request body under the shared secret.  Two rules
matter and are both enforced here:

1. Compare in constant time (:func:`hmac.compare_digest`) so a timing side
   channel cannot be used to forge a digest byte by byte.
2. Verify *before* parsing.  The body is attacker-controlled until the digest
   checks out, so nothing downstream touches it first.

Signatures and secrets are never returned, logged, or echoed in errors.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


def compute_signature(secret: str | bytes, body: bytes) -> str:
    """Return the ``sha256=<hex>`` header value GitHub would send for *body*."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(secret: str | bytes, body: bytes, header_value: str | None) -> bool:
    """Constant-time check of a GitHub ``X-Hub-Signature-256`` header.

    Returns False -- never raises -- for a missing header, a wrong or absent
    ``sha256=`` prefix, a non-hex digest, or a digest mismatch.
    """
    if not header_value or not secret:
        return False

    header_value = header_value.strip()
    if not header_value.startswith(SIGNATURE_PREFIX):
        return False

    provided = header_value[len(SIGNATURE_PREFIX) :]
    if len(provided) != 64:
        return False
    try:
        bytes.fromhex(provided)
    except ValueError:
        return False

    expected = compute_signature(secret, body)[len(SIGNATURE_PREFIX) :]
    # Both operands are fixed-length lowercase hex here, so compare_digest's
    # length-leak caveat does not apply.
    return hmac.compare_digest(expected, provided.lower())

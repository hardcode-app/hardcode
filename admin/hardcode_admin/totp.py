"""
TOTP (RFC 6238) on HOTP (RFC 4226): SHA-1, 6 digits, 30-second steps.

That is what Google Authenticator, 1Password, Authy and the rest expect.
Standard library only.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

STEP = 30  # seconds
DIGITS = 6


def generate_secret() -> str:
    """
    A new base32 secret of 160 bits, without padding.
    """
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def decode_secret(secret: str) -> bytes:
    """
    The key in a base32 secret, as people type it: any case, with spaces,
    without padding.
    """
    clean = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(clean) % 8)

    return base64.b32decode(clean + padding)


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    """
    The HOTP code for *counter* (RFC 4226, section 5.3).
    """
    digest = hmac.new(decode_secret(secret), struct.pack(">Q", counter), "sha1").digest()

    # Dynamic truncation: the last nibble picks four bytes of the digest,
    # read as a 31-bit number.
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF

    return str(code % 10**digits).zfill(digits)


def totp(secret: str, now: Optional[float] = None, step: int = STEP, digits: int = DIGITS) -> str:
    """
    The TOTP code for the time step containing *now* (the current time by default).
    """
    moment = time.time() if now is None else now
    return hotp(secret, int(moment // step), digits)


def matching_step(secret: str, code: str, now: Optional[float] = None) -> Optional[int]:
    """
    The time step *code* is valid for, or ``None``.

    The current step and its neighbours are accepted, allowing one step of
    clock drift. Callers remember the step, so that a code is accepted only
    once, as RFC 6238 requires.
    """
    candidate = str(code or "").replace(" ", "")
    if not (candidate.isdigit() and len(candidate) == DIGITS):
        return None

    moment = time.time() if now is None else now
    current = int(moment // STEP)

    for skew in (0, -1, 1):
        # Constant-time, so the comparison doesn't leak how much of a guess was right.
        if hmac.compare_digest(hotp(secret, current + skew), candidate):
            return current + skew

    return None


def verify(secret: str, code: str, now: Optional[float] = None) -> bool:
    """
    Whether *code* is valid around *now*.
    """
    return matching_step(secret, code, now) is not None


def otpauth_url(secret: str, email: str, issuer: str = "HardCode") -> str:
    """
    The ``otpauth://`` URL an authenticator app reads from the enrolment QR code.
    """
    label = f"{quote(issuer)}:{quote(email)}"
    parameters = f"secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"

    return f"otpauth://totp/{label}?{parameters}"

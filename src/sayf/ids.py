from __future__ import annotations

import secrets
import time
import uuid


def uuid7() -> uuid.UUID:
    """Return an RFC 9562 UUIDv7 using only the Python standard library."""
    timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    return uuid.UUID(int=value)


def new_id(prefix: str) -> str:
    if not prefix or any(ch.isspace() for ch in prefix):
        raise ValueError("prefix must be non-empty and contain no whitespace")
    return f"{prefix}_{uuid7()}"

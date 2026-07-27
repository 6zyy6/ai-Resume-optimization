import secrets
import time
import uuid


def new_id(prefix: str) -> str:
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    value = timestamp << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return f"{prefix}_{uuid.UUID(int=value)}"

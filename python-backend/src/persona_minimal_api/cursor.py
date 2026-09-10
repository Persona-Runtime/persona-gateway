from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class CursorError(ValueError):
    pass


@dataclass(frozen=True)
class PersonaCursor:
    owner_subject: str
    created_at: datetime
    persona_id: UUID


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def encode(cursor: PersonaCursor, key: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "owner": cursor.owner_subject,
            "created_at": cursor.created_at.isoformat(),
            "id": str(cursor.persona_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def decode(value: str, key: str, expected_owner: str) -> PersonaCursor:
    try:
        payload_part, signature_part = value.split(".", 1)
        payload, supplied_signature = _b64decode(payload_part), _b64decode(signature_part)
        expected_signature = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise CursorError("signature")
        decoded = json.loads(payload)
        if decoded["v"] != 1 or decoded["owner"] != expected_owner:
            raise CursorError("scope")
        created_at = datetime.fromisoformat(decoded["created_at"])
        if created_at.tzinfo is None:
            raise CursorError("timestamp")
        return PersonaCursor(expected_owner, created_at, UUID(decoded["id"]))
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, CursorError):
            raise
        raise CursorError("invalid") from exc

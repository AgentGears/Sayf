from __future__ import annotations

import hashlib
import json
from typing import Any

from sayf.domain import Actor, EventDraft

HASH_PREFIX = "sha256:"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def event_hash_payload(
    draft: EventDraft,
    *,
    sequence: int,
    previous_event_hash: str | None,
) -> dict[str, Any]:
    actor: Actor = draft.actor
    return {
        "event_id": draft.event_id,
        "sequence": sequence,
        "stream_id": draft.stream_id,
        "event_type": draft.event_type,
        "occurred_at": draft.occurred_at.isoformat(),
        "actor": actor.model_dump(mode="json"),
        "payload": draft.payload,
        "previous_event_hash": previous_event_hash,
    }


def compute_event_hash(
    draft: EventDraft,
    *,
    sequence: int,
    previous_event_hash: str | None,
) -> str:
    payload = event_hash_payload(
        draft,
        sequence=sequence,
        previous_event_hash=previous_event_hash,
    )
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"

from __future__ import annotations

import json
from typing import Callable

from .hashing import canonical_json_bytes
from .models import AttemptStatus, RawAttempt
from .raw import RawStore


def normalize_attempt(
    store: RawStore,
    attempt: RawAttempt,
    normalizer: Callable[[dict[str, object]], dict[str, object]],
) -> tuple[bytes, list[dict[str, object]]]:
    if attempt.status is not AttemptStatus.SUCCESS:
        raise ValueError(f"cannot normalize {attempt.status.value} attempt")
    rows: list[dict[str, object]] = []
    for digest in attempt.object_hashes:
        page = json.loads(store.read_object(digest))
        if not isinstance(page, list):
            raise ValueError("RAW page is not a list")
        rows.extend(normalizer(dict(item)) for item in page)
    payload = b"\n".join(canonical_json_bytes(item) for item in rows) + b"\n"
    return payload, rows

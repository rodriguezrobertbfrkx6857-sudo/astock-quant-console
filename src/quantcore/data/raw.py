"""Immutable, content-addressed RAW object and attempt storage."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from src.quantcore.contracts.safety import PathBoundary

from .hashing import canonical_json_bytes, sha256_bytes
from .models import AttemptStatus, Provenance, RawAttempt


_SENSITIVE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key)", re.I
)


def redact_mapping(value: dict[str, object]) -> dict[str, object]:
    return {
        key: "<redacted>" if _SENSITIVE.search(key) else item
        for key, item in value.items()
    }


class RawStore:
    def __init__(self, root: str | Path):
        self.root = PathBoundary().require_writable(root)
        self.objects = self.root / "objects" / "sha256"
        self.attempts = self.root / "attempts"
        PathBoundary().ensure_directories((self.objects, self.attempts))

    def put_object(self, payload: bytes) -> str:
        digest = sha256_bytes(payload)
        target = self.objects / digest[:2] / digest
        if target.exists():
            if target.read_bytes() != payload:
                raise RuntimeError("content-address collision")
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{digest}.{uuid4().hex}.tmp"
        temporary.write_bytes(payload)
        try:
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return digest

    def read_object(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid sha256 digest")
        target = self.objects / digest[:2] / digest
        payload = target.read_bytes()
        if sha256_bytes(payload) != digest:
            raise ValueError("RAW object hash mismatch")
        return payload

    def record_attempt(self, attempt: RawAttempt) -> Path:
        attempt = replace(attempt, request=redact_mapping(attempt.request))
        target = self.attempts / f"{attempt.attempt_id}.json"
        body = canonical_json_bytes(attempt.to_dict())
        if target.exists():
            if target.read_bytes() != body:
                raise ValueError(
                    "attempt_id is immutable and already has different content"
                )
            return target
        temporary = target.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_bytes(body)
        os.replace(temporary, target)
        return target

    def capture_json_pages(
        self,
        *,
        source_id: str,
        dataset: str,
        request: dict[str, object],
        pages: list[list[dict[str, object]]],
        provenance: Provenance,
        pages_expected: int | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> RawAttempt:
        now = datetime.now(UTC).isoformat()
        row_count = sum(len(page) for page in pages)
        if error_code or error_message:
            status = AttemptStatus.FAILED
            hashes: tuple[str, ...] = ()
            row_count = 0
        elif pages_expected is not None and len(pages) < pages_expected:
            status = AttemptStatus.PARTIAL
            hashes = tuple(
                self.put_object(canonical_json_bytes(page)) for page in pages
            )
        elif row_count == 0:
            status = AttemptStatus.EMPTY
            hashes = ()
        else:
            status = AttemptStatus.SUCCESS
            hashes = tuple(
                self.put_object(canonical_json_bytes(page)) for page in pages
            )
        attempt = RawAttempt(
            attempt_id=f"raw-{uuid4().hex}",
            source_id=source_id,
            dataset=dataset,
            requested_at=now,
            completed_at=now,
            request=redact_mapping(request),
            provenance=provenance,
            status=status,
            pages_expected=pages_expected,
            pages_received=len(pages),
            row_count=row_count,
            object_hashes=hashes,
            error_code=error_code,
            error_message=error_message,
        )
        self.record_attempt(attempt)
        return attempt

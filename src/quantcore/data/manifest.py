"""Immutable manifests and readiness derivation."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from src.quantcore.contracts.safety import PathBoundary

from .hashing import canonical_hash, canonical_json_bytes, sha256_file
from .models import (
    AttemptStatus,
    DataLayer,
    DataManifest,
    Provenance,
    QualityStatus,
    Readiness,
)


def derive_readiness(
    *,
    provenance: Provenance,
    attempt_status: AttemptStatus,
    row_count: int,
    pagination_complete: bool,
    quality_status: QualityStatus | None,
) -> tuple[Readiness, tuple[str, ...]]:
    reasons = []
    if provenance is not Provenance.REAL:
        reasons.append(f"PROVENANCE_{provenance.value}")
    if attempt_status is not AttemptStatus.SUCCESS:
        reasons.append(f"ATTEMPT_{attempt_status.value}")
    if row_count <= 0:
        reasons.append("EMPTY_DATASET")
    if not pagination_complete:
        reasons.append("PAGINATION_INCOMPLETE")
    if quality_status is not QualityStatus.PASS:
        reasons.append("QUALITY_NOT_PASS")
    if reasons:
        readiness = (
            Readiness.RESEARCH_ONLY
            if provenance is Provenance.SYNTHETIC
            and attempt_status is AttemptStatus.SUCCESS
            and quality_status is QualityStatus.PASS
            else Readiness.BLOCKED
        )
    else:
        readiness = Readiness.REAL_READY
    return readiness, tuple(reasons)


class ManifestStore:
    def __init__(self, root: str | Path, object_root: str | Path):
        self.root = PathBoundary().require_writable(root)
        self.object_root = PathBoundary().require_writable(object_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def freeze(self, manifest: DataManifest) -> DataManifest:
        digest = canonical_hash(manifest.payload())
        frozen = replace(manifest, manifest_hash=digest)
        target = self.root / f"{frozen.manifest_id}.json"
        body = canonical_json_bytes({**frozen.payload(), "manifest_hash": digest})
        if target.exists():
            if target.read_bytes() != body:
                raise ValueError("manifest_id is immutable and already differs")
            return frozen
        temporary = target.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_bytes(body)
        os.replace(temporary, target)
        return frozen

    def load(self, manifest_id: str, *, verify_objects: bool = True) -> DataManifest:
        raw = json.loads((self.root / f"{manifest_id}.json").read_text("utf-8"))
        stored_hash = raw.pop("manifest_hash")
        if canonical_hash(raw) != stored_hash:
            raise ValueError("manifest hash mismatch")
        raw.update(
            {
                "layer": DataLayer(raw["layer"]),
                "provenance": Provenance(raw["provenance"]),
                "attempt_status": AttemptStatus(raw["attempt_status"]),
                "quality_status": QualityStatus(raw["quality_status"])
                if raw["quality_status"]
                else None,
                "readiness": Readiness(raw["readiness"]),
                "manifest_hash": stored_hash,
                "object_hashes": tuple(raw["object_hashes"]),
                "upstream_manifest_ids": tuple(raw["upstream_manifest_ids"]),
                "reasons": tuple(raw["reasons"]),
            }
        )
        manifest = DataManifest(**raw)
        if verify_objects:
            for digest in manifest.object_hashes:
                path = self.object_root / digest[:2] / digest
                if not path.is_file() or sha256_file(path) != digest:
                    raise ValueError(f"manifest object missing or changed: {digest}")
        return manifest

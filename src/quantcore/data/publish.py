from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import uuid4

from src.quantcore.contracts.safety import PathBoundary

from .manifest import ManifestStore
from .models import Readiness


class PublicationStore:
    def __init__(self, root: str | Path, manifests: ManifestStore):
        self.root = PathBoundary().require_writable(root)
        self.manifests = manifests
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(self, dataset: str, manifest_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", dataset):
            raise ValueError("unsafe publication dataset")
        manifest = self.manifests.load(manifest_id)
        if manifest.dataset != dataset:
            raise ValueError("publication dataset mismatch")
        if manifest.readiness is not Readiness.REAL_READY:
            raise ValueError(f"manifest is not publishable: {manifest.readiness.value}")
        target = self.root / f"{dataset}.json"
        payload = json.dumps(
            {
                "dataset": dataset,
                "manifest_id": manifest_id,
                "manifest_hash": manifest.manifest_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        temporary = target.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
        return target

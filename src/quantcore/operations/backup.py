from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from src.quantcore.contracts.safety import PathBoundary


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class BackupManager:
    def __init__(
        self, control: str | Path, accounting: str | Path, backup_root: str | Path
    ):
        boundary = PathBoundary()
        self.control = boundary.require_writable(control)
        self.accounting = boundary.require_writable(accounting)
        self.backup_root = boundary.require_writable(backup_root)

    def create(self, backup_id: str) -> Path:
        target = PathBoundary().require_writable(self.backup_root / backup_id)
        if target.exists():
            raise FileExistsError(target)
        target.mkdir(parents=True)
        files = {}
        try:
            for name, source in (
                ("control.sqlite", self.control),
                ("accounting.sqlite", self.accounting),
            ):
                output = target / name
                src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
                dst = sqlite3.connect(output)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                check = sqlite3.connect(f"file:{output.as_posix()}?mode=ro", uri=True)
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
                versions = [
                    x[0]
                    for x in check.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                check.close()
                files[name] = {
                    "sha256": _hash(output),
                    "bytes": output.stat().st_size,
                    "integrity": integrity,
                    "schema_versions": versions,
                }
            manifest = {
                "backup_id": backup_id,
                "created_at": datetime.now(UTC).isoformat(),
                "files": files,
            }
            raw = (
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n"
            )
            manifest_path = target / "manifest.json"
            temp = target / "manifest.tmp"
            temp.write_text(raw, "utf-8")
            os.replace(temp, manifest_path)
            return manifest_path
        except Exception:
            (target / "INCOMPLETE").write_text("backup failed", "utf-8")
            raise

    def restore(
        self, manifest_path: str | Path, destination: str | Path
    ) -> dict[str, str]:
        manifest_path = PathBoundary().require_readable(manifest_path)
        destination = PathBoundary().require_writable(destination)
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError("restore destination must be new or empty")
        manifest = json.loads(manifest_path.read_text("utf-8"))
        source_root = manifest_path.parent
        for name, meta in manifest["files"].items():
            source = source_root / name
            if _hash(source) != meta["sha256"]:
                raise RuntimeError(f"backup hash mismatch: {name}")
        destination.mkdir(parents=True, exist_ok=True)
        restored = {}
        for name, meta in manifest["files"].items():
            source = sqlite3.connect(
                f"file:{(source_root/name).as_posix()}?mode=ro", uri=True
            )
            target = sqlite3.connect(destination / name)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            if _hash(destination / name) != meta["sha256"]:
                raise RuntimeError("restored database hash mismatch")
            check = sqlite3.connect(destination / name)
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            check.close()
            if integrity != "ok":
                raise RuntimeError("restored database integrity failure")
            restored[name] = meta["sha256"]
        return restored


class FullBackupManager:
    """Create and verify a self-contained, isolated V2 project backup.

    The backup deliberately excludes ``artifacts/v2/backups`` itself and
    disposable recovery/test directories.  Every included file is content
    hashed.  Restore always targets a new directory and never overwrites a
    project file in place.
    """

    _RELATIVE_FILES = (
        Path("config/settings.json"),
        Path("config/v2/settings.json"),
        Path("config/v2/settings.schema.json"),
        Path("config/v2/initial_research_queue.json"),
        Path("config/v2/oos_known_lower_bound.json"),
        Path("config/v2/operational_profile.json"),
        Path("config/v2/universe_profiles.json"),
        Path("state/v2/control.sqlite"),
        Path("state/v2/accounting.sqlite"),
        Path("state/v2/market_catalog.sqlite"),
        Path("state/v2/product_catalog.sqlite"),
        Path("state/v2/legacy_archive_catalog.sqlite"),
        Path("README_USER.md"),
        Path("START_QUANT_LAB.bat"),
        Path("STOP_QUANT_LAB.bat"),
        Path("RUN_EOD_NOW.bat"),
        Path("RUN_RESEARCH_NOW.bat"),
        Path("RUN_REAL_DEMO.bat"),
        Path("RUN_WATCH_NOW.bat"),
        Path("RUN_FULL_PRODUCT_DEMO.bat"),
        Path("CHECK_SYSTEM_STATUS.bat"),
        Path("start_v2.bat"),
        Path("start_web.bat"),
        Path("dashboard/index.html"),
        Path("dashboard/package.json"),
        Path("dashboard/package-lock.json"),
        Path("dashboard/vite.config.ts"),
        Path("dashboard/tsconfig.json"),
        Path("dashboard/tailwind.config.js"),
    )
    _RELATIVE_DIRS = (
        Path("data/v2/legacy_archive"),
        Path("data/v2/raw"),
        Path("data/v2/manifests"),
        Path("data/v2/published"),
        Path("data/v2/market_store"),
        Path("artifacts/v2/evidence"),
        Path("artifacts/v2/rollback"),
        Path("src/quantcore"),
        Path("scripts/v2"),
        Path("quantweb"),
        Path("reference_engine"),
        Path("qforge"),
        Path("dashboard/src"),
        Path("docs/integrated_v2"),
        Path("dashboard/dist"),
    )
    _EXCLUDED_PARTS = {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        "cache",
        "node_modules",
        "test_tmp",
        "recovery",
    }

    def __init__(self, project_root: str | Path, backup_root: str | Path):
        boundary = PathBoundary()
        self.project_root = boundary.require_writable(project_root)
        self.backup_root = boundary.require_writable(backup_root)
        if not self.project_root.is_dir():
            raise FileNotFoundError(self.project_root)

    def _files(self) -> tuple[tuple[Path, Path], ...]:
        boundary = PathBoundary()
        selected: dict[str, tuple[Path, Path]] = {}
        for relative in self._RELATIVE_FILES:
            source = boundary.require_readable(self.project_root / relative)
            if not source.is_file():
                raise FileNotFoundError(source)
            selected[relative.as_posix()] = (relative, source)
        for root in self._RELATIVE_DIRS:
            source_root = boundary.require_readable(self.project_root / root)
            if not source_root.is_dir():
                continue
            for source in source_root.rglob("*"):
                if source.is_file() and not self._EXCLUDED_PARTS.intersection(source.parts):
                    relative = source.relative_to(self.project_root)
                    selected[relative.as_posix()] = (relative, source)
        return tuple(selected[key] for key in sorted(selected))

    @staticmethod
    def _copy(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)

    def create(self, backup_id: str) -> Path:
        if not backup_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in backup_id):
            raise ValueError("backup_id contains unsafe characters")
        target = PathBoundary().require_writable(self.backup_root / backup_id)
        if target.exists():
            raise FileExistsError(target)
        target.mkdir(parents=True)
        entries: list[dict[str, object]] = []
        try:
            for relative, source in self._files():
                output = target / relative
                self._copy(source, output)
                entries.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": _hash(output),
                        "bytes": output.stat().st_size,
                    }
                )
            manifest = {
                "backup_id": backup_id,
                "kind": "FULL_V2",
                "created_at": datetime.now(UTC).isoformat(),
                "project_root": str(self.project_root),
                "excluded": [
                    "artifacts/v2/backups",
                    "state/v2/recovery",
                    "state/v2/test_tmp",
                    "node_modules and disposable/cache directories",
                    "config/v2/ai.local.json (may contain a credential)",
                ],
                "files": entries,
            }
            manifest_path = target / "manifest.json"
            temporary = target / "manifest.tmp"
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, manifest_path)
            return manifest_path
        except Exception:
            (target / "INCOMPLETE").write_text("full backup failed", encoding="utf-8")
            raise

    def restore(self, manifest_path: str | Path, destination: str | Path) -> dict[str, object]:
        manifest_path = PathBoundary().require_readable(manifest_path)
        destination = PathBoundary().require_writable(destination)
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError("restore destination must be new or empty")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") != "FULL_V2" or not isinstance(manifest.get("files"), list):
            raise ValueError("invalid full V2 backup manifest")
        source_root = manifest_path.parent
        checked: list[dict[str, object]] = []
        for item in manifest["files"]:
            relative_text = str(item.get("path", ""))
            relative = Path(relative_text)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("backup contains unsafe relative path")
            source = source_root / relative
            if not source.is_file() or _hash(source) != str(item.get("sha256")):
                raise RuntimeError(f"backup hash mismatch: {relative_text}")
            checked.append({"path": relative_text, "sha256": str(item["sha256"]), "bytes": source.stat().st_size})
        destination.mkdir(parents=True, exist_ok=True)
        for item in checked:
            relative = Path(str(item["path"]))
            output = destination / relative
            self._copy(source_root / relative, output)
            if _hash(output) != str(item["sha256"]):
                raise RuntimeError(f"restored file hash mismatch: {relative}")
            if output.suffix == ".sqlite":
                conn = sqlite3.connect(f"file:{output.as_posix()}?mode=ro", uri=True)
                try:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    conn.close()
                if integrity != "ok":
                    raise RuntimeError(f"restored database integrity failure: {relative}")
        return {"file_count": len(checked), "files": checked, "destination": str(destination)}

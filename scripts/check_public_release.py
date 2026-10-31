"""Check distributable source, reporting locations only, never matched values.

This is a focused guard, not a guarantee that arbitrary secrets are detectable.
Tracked files are checked even when they match .gitignore.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "__pycache__",
                ".pytest_cache", ".mypy_cache", ".ruff_cache", "data", "state", "logs", "artifacts"}
FORBIDDEN_DIRS = {"data", "state", "logs", "artifacts", "tushare15000", "credentials", ".git", "node_modules", "__pycache__"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".parquet", ".csv", ".xlsx", ".zip", ".log", ".pem", ".key", ".p12", ".pfx", ".pyc"}
PATTERNS = {
    "provider-key": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})\b"),
    "relay-key": re.compile(r"\btsr_[A-Za-z0-9_-]{24,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential-url": re.compile(r"https?://[^\s/:]+:[^\s/@]+@"),
    "user-home": re.compile(r"(?i)(?:[a-z]:[/\\]Users[/\\]|/Users/|/home/)[a-z0-9_.-]+"),
    "assigned-secret": re.compile(r'''(?i)["']?(?:api_key|password|access_token|client_secret)["']?\s*[:=]\s*["'](?!\$\{|YOUR_|EXAMPLE_|test|fake|dummy|sentinel|\*|<|api_key)[A-Za-z0-9_+/=-]{20,}["']'''),
}


def source_files() -> list[Path]:
    if (ROOT / ".git").exists():
        output = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT)
        return sorted({ROOT / name.decode("utf-8") for name in output.split(b"\0") if name})
    return sorted(p for p in ROOT.rglob("*") if p.is_file()
                  and p.relative_to(ROOT).parts[0] not in {"data", "state", "logs", "artifacts"}
                  and not (set(p.relative_to(ROOT).parts) & (IGNORED_DIRS - {"data", "state", "logs", "artifacts"})))


def main() -> int:
    failures: list[str] = []
    files = source_files()
    for path in files:
        rel = path.relative_to(ROOT)
        name = rel.as_posix()
        if path.is_symlink():
            failures.append(f"{name}: symbolic link requires manual review")
            continue
        if (rel.parts[0] in {"data", "state", "logs", "artifacts"}
                or set(rel.parts[:-1]) & (FORBIDDEN_DIRS - {"data", "state", "logs", "artifacts"})
                or path.suffix.lower() in FORBIDDEN_SUFFIXES
                or ".local." in path.name or (path.name.startswith(".env") and path.name != ".env.example")):
            failures.append(f"{name}: forbidden private/generated file")
        if path.stat().st_size > 10_000_000:
            failures.append(f"{name}: unexpectedly large source file")
        try:
            content = path.read_text("utf-8-sig")
        except UnicodeDecodeError:
            failures.append(f"{name}: unexpected binary file")
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(content):
                failures.append(f"{name}:{content.count(chr(10), 0, match.start()) + 1}: {label}")
    print(f"Checked {len(files)} distributable files; {len(failures)} findings.")
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())

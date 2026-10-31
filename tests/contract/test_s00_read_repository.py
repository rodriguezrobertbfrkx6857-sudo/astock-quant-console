from __future__ import annotations

import hashlib
import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path

from quantweb.read_repository import OfflineSQLiteRepository
from src.quantcore.contracts.safety import PROJECT_ROOT, SafetyViolation


class ReadRepositoryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PROJECT_ROOT / "state" / "v2" / "test_tmp" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.db_path = self.root / "fixture.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO facts(value) VALUES ('known')")
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        # The exact resolved target is created by setUp under the main project.
        shutil.rmtree(self.root)

    def _digest(self) -> str:
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()

    def test_select_works_without_changing_the_database(self) -> None:
        before = self._digest()
        repo = OfflineSQLiteRepository(self.root)
        conn = repo.connect("fixture.db")
        try:
            row = repo.one(conn, "SELECT value FROM facts WHERE id=?", (1,))
        finally:
            conn.close()
        self.assertEqual(row["value"], "known")
        self.assertEqual(self._digest(), before)

    def test_ddl_and_dml_are_denied(self) -> None:
        repo = OfflineSQLiteRepository(self.root)
        conn = repo.connect("fixture.db")
        try:
            for sql in (
                "CREATE TABLE forbidden(id INTEGER)",
                "INSERT INTO facts(value) VALUES ('bad')",
                "UPDATE facts SET value='bad'",
                "DELETE FROM facts",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql)
        finally:
            conn.close()

    def test_missing_database_is_not_created(self) -> None:
        repo = OfflineSQLiteRepository(self.root)
        missing = self.root / "missing.db"
        with self.assertRaises(FileNotFoundError):
            repo.connect("missing.db")
        self.assertFalse(missing.exists())

    def test_path_traversal_is_denied(self) -> None:
        repo = OfflineSQLiteRepository(self.root)
        with self.assertRaises(SafetyViolation):
            repo.connect("../fixture.db")

    def test_nonempty_wal_is_fail_closed(self) -> None:
        wal = Path(str(self.db_path) + "-wal")
        wal.write_bytes(b"uncheckpointed")
        repo = OfflineSQLiteRepository(self.root)
        with self.assertRaises(SafetyViolation):
            repo.connect("fixture.db")

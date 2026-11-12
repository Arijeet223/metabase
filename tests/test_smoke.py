"""Small smoke test for trigger-based UPDATE auditing and schema comparison."""
import os
import tempfile
import unittest
from pathlib import Path

from app.db import init_meta_db, connect_meta
from app.metadata import install_audit_triggers, sync_source


class MetaBaseSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.meta_path = root / "meta.db"
        self.source_path = root / "source.db"
        os.environ["METABASE_DB_PATH"] = str(self.meta_path)
        import app.db as db_module
        db_module.META_DB_PATH = self.meta_path
        init_meta_db()
        import sqlite3
        c = sqlite3.connect(self.source_path)
        c.executescript("""
        CREATE TABLE Employees(employee_id INTEGER PRIMARY KEY, name TEXT, salary REAL);
        INSERT INTO Employees VALUES(1,'Test User',100.0);
        """)
        c.commit()
        c.close()
        conn = connect_meta()
        conn.execute("""INSERT INTO DATA_SOURCE(source_name,db_type,database_path,database_name)
                        VALUES('Test','sqlite',?, 'source.db')""", (str(self.source_path),))
        conn.commit()
        self.source_id = conn.execute("SELECT source_id FROM DATA_SOURCE").fetchone()["source_id"]
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_is_captured_once(self):
        sync_source(self.source_id)  # baseline + trigger install
        import sqlite3
        c = sqlite3.connect(self.source_path)
        c.execute("UPDATE Employees SET salary=125.0 WHERE employee_id=1")
        c.commit()
        c.close()
        result = sync_source(self.source_id)
        self.assertEqual(result["imported_data_events"], 1)
        # Re-sync must not duplicate the same event.
        result2 = sync_source(self.source_id)
        self.assertEqual(result2["imported_data_events"], 0)
        conn = connect_meta()
        row = conn.execute("SELECT * FROM DATA_CHANGE_LOG").fetchone()
        conn.close()
        self.assertEqual(row["operation"], "UPDATE")
        self.assertIn("100.0", row["old_values"])
        self.assertIn("125.0", row["new_values"])

    def test_insert_delete_and_schema_change(self):
        sync_source(self.source_id)
        import sqlite3
        c = sqlite3.connect(self.source_path)
        c.execute("INSERT INTO Employees VALUES(2,'Another User',200.0)")
        c.execute("UPDATE Employees SET salary=150.0 WHERE employee_id=1")
        c.execute("DELETE FROM Employees WHERE employee_id=2")
        c.execute("ALTER TABLE Employees ADD COLUMN department TEXT")
        c.commit()
        c.close()
        result = sync_source(self.source_id)
        self.assertEqual(result["imported_data_events"], 3)
        self.assertEqual(result["schema_changes_detected"], 1)
        conn = connect_meta()
        operations = [r[0] for r in conn.execute("SELECT operation FROM DATA_CHANGE_LOG ORDER BY data_change_id")]
        schema_change = conn.execute("SELECT change_type FROM SCHEMA_CHANGE_LOG ORDER BY change_id DESC LIMIT 1").fetchone()[0]
        conn.close()
        self.assertEqual(operations, ["INSERT", "UPDATE", "DELETE"])
        self.assertEqual(schema_change, "COLUMN_ADDED")


if __name__ == "__main__":
    unittest.main()

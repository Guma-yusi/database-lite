import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from minidb import Database
from minidb.errors import SemanticError, StorageError


class EngineAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name, buffer_pages=2)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_complete_base_crud_path(self):
        self.db.execute("CREATE TABLE users(id INT,name VARCHAR(12));")
        self.db.execute("INSERT INTO users(id,name) VALUES(1,'Alice'); INSERT INTO users(name,id) VALUES('Bob',2);")
        self.assertEqual(self.db.execute("SELECT name FROM users WHERE id>=2;")[0]["rows"], [{"name": "Bob"}])
        self.assertEqual(self.db.execute("DELETE FROM users WHERE id=1;")[0]["affected"], 1)
        self.assertEqual(self.db.execute("SELECT id FROM users;")[0]["rows"], [{"id": 2}])

    def test_update_can_move_a_growing_record_and_order_by_unprojected_column(self):
        self.db.execute("CREATE TABLE notes(id INT,text VARCHAR(100));")
        for index in range(80):
            self.db.execute(f"INSERT INTO notes(id,text) VALUES({index},'x');")
        self.db.execute("UPDATE notes SET text='a much longer value' WHERE id=20;")
        rows = self.db.execute("SELECT text FROM notes WHERE id>=20 AND id<=21 ORDER BY id DESC;")[0]["rows"]
        self.assertEqual(rows, [{"text": "x"}, {"text": "a much longer value"}])

    def test_all_metadata_and_rows_survive_restart(self):
        self.db.execute("CREATE TABLE saved(id INT,name VARCHAR(10)); INSERT INTO saved(id,name) VALUES(7,'persist');")
        root = self.temp.name
        self.db.close()
        self.db = Database(root, buffer_pages=2)
        self.assertEqual(self.db.execute("SELECT * FROM saved;")[0]["rows"], [{"id": 7, "name": "persist"}])
        self.assertFalse((Path(root) / "catalog.json").exists())

    def test_system_catalog_is_queryable_but_read_only(self):
        self.db.execute("CREATE TABLE demo(id INT,label VARCHAR(4));")
        rows = self.db.execute(
            "SELECT column_name FROM pg_catalog WHERE table_name='demo' ORDER BY column_order;"
        )[0]["rows"]
        self.assertEqual(rows, [{"column_name": "id"}, {"column_name": "label"}])
        with self.assertRaises(SemanticError):
            self.db.execute("DELETE FROM pg_catalog;")

    def test_oversized_row_fails_without_leaking_an_allocated_page(self):
        self.db.execute("CREATE TABLE large_payload(id INT,text VARCHAR);")
        before = self.db.stats()["allocated_pages"]
        sql = "INSERT INTO large_payload(id,text) VALUES(1,'" + ("x" * 5000) + "');"
        with self.assertRaises(StorageError):
            self.db.execute(sql)
        self.assertEqual(self.db.stats()["allocated_pages"], before)

    def test_failed_multi_statement_input_stops_before_later_statements(self):
        self.db.execute("CREATE TABLE guarded(id INT);")
        with self.assertRaises(SemanticError):
            self.db.execute("INSERT INTO guarded(id) VALUES(1); INSERT INTO missing(id) VALUES(2); INSERT INTO guarded(id) VALUES(3);")
        self.assertEqual(self.db.execute("SELECT id FROM guarded;")[0]["rows"], [{"id": 1}])

    def test_cli_executes_a_utf8_sql_file(self):
        script = Path(self.temp.name) / "验收.sql"
        script.write_text("CREATE TABLE cli_table(id INT); INSERT INTO cli_table(id) VALUES(9);", encoding="utf-8")
        data = Path(self.temp.name) / "cli_data"
        completed = subprocess.run(
            [sys.executable, "minidb_cli.py", "--data", str(data), "--file", str(script)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("1 row inserted", completed.stdout)
        cli_db = Database(data)
        try:
            self.assertEqual(cli_db.execute("SELECT id FROM cli_table;")[0]["rows"], [{"id": 9}])
        finally:
            cli_db.close()

    def test_table_page_mapping_and_prefetch_are_visible_through_engine(self):
        self.db.execute("CREATE TABLE bulk(id INT,name VARCHAR(20));")
        self.db.execute("".join(f"INSERT INTO bulk(id,name) VALUES({i},'row{i}');" for i in range(300)))
        self.assertGreaterEqual(len(self.db.store.get_table_pages("bulk")), 2)
        self.db.store.cache.events.clear()
        rows = self.db.execute("SELECT id FROM bulk ORDER BY id DESC;")[0]["rows"]
        self.assertEqual(rows[0], {"id": 299})
        self.assertTrue(any(event["event"] in ("prefetch", "prefetch-skip") for event in self.db.store.cache.events))


if __name__ == "__main__":
    unittest.main()

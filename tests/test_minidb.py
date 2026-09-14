import tempfile
import unittest
from pathlib import Path

from minidb import Database
from minidb.errors import LexicalError, SemanticError, SyntaxError
from minidb.lexer import Lexer
from minidb.parser import Parser
from minidb.storage.disk import DiskManager
from minidb.storage.page import SlottedPage


class CompilerTests(unittest.TestCase):
    def test_token_position_comments_strings_and_case(self):
        tokens=Lexer("-- hi\nSeLeCt name FROM t WHERE name != 'Tom''s';").tokenize()
        self.assertEqual(tokens[0].lexeme,"SELECT");self.assertEqual((tokens[0].location.line,tokens[0].location.column),(2,1))
        self.assertEqual(next(t.value for t in tokens if t.lexeme.startswith("'")),"Tom's")
    def test_precedence(self):
        stmt=Parser("SELECT * FROM t WHERE a=1 OR b=2 AND c=3;").parse_all()[0]
        self.assertEqual(stmt.where.operator,"OR");self.assertEqual(stmt.where.right.operator,"AND")
    def test_update_and_order_by_ast(self):
        update,select=Parser("UPDATE t SET score=score+5,name='B' WHERE id=2; SELECT name FROM t ORDER BY score DESC,name;").parse_all()
        self.assertEqual([name for name,_ in update.assignments],["score","name"])
        self.assertEqual([(item.column,item.direction) for item in select.order_by],[("score","DESC"),("name","ASC")])
    def test_inspect_multiple_dependent_statements_without_execution(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as d:
            db=Database(d)
            result=db.inspect("CREATE TABLE fresh(id INT); INSERT INTO fresh(id) VALUES(1); SELECT * FROM fresh;")
            self.assertEqual([item["semantic"] for item in result],["passed","passed","passed"])
            self.assertEqual([item["tokens"][0]["lexeme"] for item in result],["CREATE","INSERT","SELECT"])
            self.assertFalse(db.catalog.exists("fresh"));db.close()
    def test_precise_errors(self):
        with self.assertRaises(LexicalError) as e:Lexer("SELECT @;").tokenize()
        self.assertEqual(e.exception.location.column,8)
        with self.assertRaises(SyntaxError):Parser("SELECT a FROM t WHERE a AND;").parse_all()


class StorageTests(unittest.TestCase):
    def test_page_slot_and_tombstone(self):
        p=SlottedPage(0);slot=p.insert("中文记录".encode());self.assertEqual(p.read(slot).decode(),"中文记录")
        self.assertTrue(p.delete(slot));self.assertIsNone(p.read(slot))
    def test_page_id_maps_to_file_offset(self):
        with tempfile.TemporaryDirectory() as d:
            disk=DiskManager(Path(d)/"data.syp");a=disk.allocate();b=disk.allocate()
            self.assertEqual((a.page_id,b.page_id,disk.path.stat().st_size),(0,1,8192))


class EndToEndTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.db=Database(self.tmp.name,2)
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_pipeline_crud_and_optimization(self):
        self.db.execute("CREATE TABLE student(id INT,name VARCHAR(20),age INT);")
        self.db.execute("INSERT INTO student(id,name,age) VALUES (1,'Alice',20);")
        info=self.db.inspect("SELECT name FROM student WHERE 1=1 AND age>10+8;")[0]
        self.assertEqual(info["semantic"],"passed")
        self.assertIn("10",info["plan_before"]);self.assertIn("18",info["plan_after"]);self.assertNotIn("1 = 1",info["plan_after"])
        result=self.db.execute("SELECT name FROM student WHERE age>18;")[0]
        self.assertEqual(result["rows"],[{"name":"Alice"}])
        self.assertEqual(self.db.execute("DELETE FROM student WHERE id=1;")[0]["affected"],1)
    def test_semantic_type_and_name_checks(self):
        self.db.execute("CREATE TABLE t(id INT,name VARCHAR(10));")
        with self.assertRaises(SemanticError):self.db.execute("SELECT missing FROM t;")
        with self.assertRaises(SemanticError):self.db.execute("INSERT INTO t(id,name) VALUES ('bad',1);")
        with self.assertRaises(SemanticError):self.db.execute("UPDATE t SET missing=1;")
        with self.assertRaises(SemanticError):self.db.execute("DELETE FROM pg_catalog;")
    def test_create_and_varchar_constraints_are_semantic_errors(self):
        self.db.execute("CREATE TABLE t(id INT,name VARCHAR(3));")
        self.db.execute("CREATE TABLE unlimited(name VARCHAR);")
        with self.assertRaises(SemanticError):self.db.inspect("CREATE TABLE t(other INT);")
        with self.assertRaises(SemanticError):self.db.execute("INSERT INTO t(id,name) VALUES(1,'long');")
        self.db.execute("INSERT INTO t(id,name) VALUES(1,'ok');")
        with self.assertRaises(SemanticError):self.db.execute("UPDATE t SET name='long' WHERE id=1;")
        with self.assertRaises(SemanticError):self.db.inspect("CREATE TABLE bad(name VARCHAR(0));")
    def test_system_catalog_is_a_multi_page_slotted_table(self):
        for i in range(80):self.db.execute(f"CREATE TABLE table_{i}(id INT,name VARCHAR(20));")
        rows=self.db.execute("SELECT table_name,column_name FROM pg_catalog WHERE table_name='table_79' ORDER BY column_order;")[0]["rows"]
        self.assertEqual(rows,[{"table_name":"table_79","column_name":"id"},{"table_name":"table_79","column_name":"name"}])
        self.assertGreaterEqual(self.db.stats()["allocated_pages"],2)
        root=self.tmp.name;self.db.close();self.db=Database(root,2)
        self.assertEqual(self.db.catalog.table("table_79").columns[1].name,"name")
    def test_update_order_by_and_system_catalog(self):
        self.db.execute("CREATE TABLE scores(id INT,name VARCHAR(10),score INT); INSERT INTO scores(id,name,score) VALUES(1,'A',70); INSERT INTO scores(id,name,score) VALUES(2,'B',80); INSERT INTO scores(id,name,score) VALUES(3,'C',80);")
        changed=self.db.execute("UPDATE scores SET score=score+5 WHERE id=2;")[0]
        self.assertEqual(changed["affected"],1)
        rows=self.db.execute("SELECT name,score FROM scores ORDER BY score DESC,name ASC;")[0]["rows"]
        self.assertEqual(rows,[{"name":"B","score":85},{"name":"C","score":80},{"name":"A","score":70}])
        catalog=self.db.execute("SELECT table_name,column_name FROM pg_catalog WHERE table_name='scores' ORDER BY column_name;")[0]["rows"]
        self.assertEqual([r["column_name"] for r in catalog],["id","name","score"])
    def test_duplicate_bulk_delete_and_restart_persistence(self):
        self.db.execute("CREATE TABLE bulk(id INT,name VARCHAR(20));")
        with self.assertRaises(SemanticError):self.db.execute("CREATE TABLE bulk(other INT);")
        inserts="".join(f"INSERT INTO bulk(id,name) VALUES({i},'row{i}');" for i in range(300))
        self.db.execute(inserts)
        rows=self.db.execute("SELECT id FROM bulk WHERE id>=290 ORDER BY id DESC;")[0]["rows"]
        self.assertEqual([r["id"] for r in rows],list(range(299,289,-1)))
        self.assertEqual(self.db.execute("DELETE FROM bulk WHERE id>=295;")[0]["affected"],5)
        root=self.tmp.name;self.db.close();self.db=Database(root,2)
        remaining=self.db.execute("SELECT id FROM bulk WHERE id>=290 ORDER BY id;")[0]["rows"]
        self.assertEqual([r["id"] for r in remaining],[290,291,292,293,294])
        self.assertFalse((Path(root)/"catalog.json").exists())
        self.assertEqual(self.db.store.read_page(0)[:4],b"SYP1")
        self.assertGreaterEqual(self.db.stats()["allocated_pages"],3)
    def test_seq_scan_prefetches_next_page(self):
        self.db.execute("CREATE TABLE bulk(id INT,name VARCHAR(20));")
        inserts="".join(f"INSERT INTO bulk(id,name) VALUES({i},'row{i}');" for i in range(300))
        self.db.execute(inserts)
        root=self.tmp.name;self.db.close();self.db=Database(root,2)
        self.db.store.cache.events.clear()
        rows=self.db.execute("SELECT id FROM bulk;")[0]["rows"]
        self.assertEqual(len(rows),300)
        self.assertTrue(any(event["event"]=="prefetch" for event in self.db.store.cache.events))
        self.assertGreater(self.db.stats()["hits"],0)
    def test_storage_integration_exposes_insert_scan_delete_effects(self):
        self.db.execute("CREATE TABLE bulk(id INT,name VARCHAR(20));")
        inserts="".join(f"INSERT INTO bulk(id,name) VALUES({i},'row{i}');" for i in range(300))
        before=self.db.stats()
        self.db.execute(inserts)
        pages=self.db.store.get_table_pages("bulk")
        after_insert=self.db.stats()
        self.assertGreaterEqual(len(pages),2)
        self.assertGreater(after_insert["allocated_pages"],before["allocated_pages"])
        self.db.store.cache.events.clear()
        selected=self.db.execute("SELECT id FROM bulk WHERE id>=295 ORDER BY id;")[0]["rows"]
        self.assertEqual([r["id"] for r in selected],[295,296,297,298,299])
        self.assertTrue(any(event["event"] in ("prefetch","prefetch-skip") for event in self.db.store.cache.events))
        self.assertGreater(self.db.stats()["hits"],0)
        self.db.store.cache.events.clear()
        deleted=self.db.execute("DELETE FROM bulk WHERE id>=298;")[0]
        self.assertEqual(deleted["affected"],2)
        delete_events=[event["event"] for event in self.db.store.cache.events]
        self.assertIn("dirty",delete_events)
        self.assertIn("flush",delete_events)
    def test_database_uses_os_table_mapping_and_recovers_it_after_restart(self):
        self.db.execute("CREATE TABLE mapped(id INT,name VARCHAR(20));")
        inserts="".join(f"INSERT INTO mapped(id,name) VALUES({i},'row{i}');" for i in range(300))
        self.db.execute(inserts)
        pages=self.db.store.get_table_pages("mapped")
        self.assertGreaterEqual(len(pages),2)
        self.assertEqual(self.db.catalog.table("mapped").first_page,pages[0])
        root=self.tmp.name;self.db.close();self.db=Database(root,2)
        recovered_pages=self.db.store.get_table_pages("mapped")
        self.assertEqual(recovered_pages,pages)
        rows=self.db.execute("SELECT id FROM mapped WHERE id>=297 ORDER BY id;")[0]["rows"]
        self.assertEqual([r["id"] for r in rows],[297,298,299])


if __name__=="__main__":unittest.main()

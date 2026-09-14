import random,string,tempfile,unittest
from minidb import Database
from minidb.errors import MiniDBError


class FuzzTests(unittest.TestCase):
    def test_random_sql_never_crashes_with_internal_exception(self):
        rng=random.Random(20260907)
        alphabet=string.ascii_letters+string.digits+" '_(),;=<>!+-*/\n@"
        with tempfile.TemporaryDirectory() as d:
            db=Database(d)
            for _ in range(300):
                sql="".join(rng.choice(alphabet) for _ in range(rng.randint(0,120)))
                try:db.inspect(sql)
                except MiniDBError:pass
                except Exception as exc:self.fail(f"unclassified crash for {sql!r}: {type(exc).__name__}: {exc}")

    def test_random_execute_after_bootstrap_only_raises_project_errors(self):
        rng=random.Random(20260914)
        fragments=["SELECT * FROM fuzz;","INSERT INTO fuzz(id,name) VALUES(1,'a');","DELETE FROM fuzz WHERE id=1;","UPDATE fuzz SET name='b' WHERE id=1;","SELECT name FROM fuzz WHERE id>=1 ORDER BY name DESC;","BROKEN ;","SELECT @;"]
        with tempfile.TemporaryDirectory() as d:
            db=Database(d)
            db.execute("CREATE TABLE fuzz(id INT,name VARCHAR(5));")
            for _ in range(120):
                sql=" ".join(rng.choice(fragments) for _ in range(rng.randint(1,3)))
                try:db.execute(sql)
                except MiniDBError:pass
                except Exception as exc:self.fail(f"unclassified execute crash for {sql!r}: {type(exc).__name__}: {exc}")

if __name__=="__main__":unittest.main()

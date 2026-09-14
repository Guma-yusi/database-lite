import unittest

from minidb.ast import CreateTableStmt, DeleteStmt, InsertStmt, SelectStmt, UpdateStmt
from minidb.catalog import ColumnSchema, PagedCatalog, TableSchema
from minidb.errors import LexicalError, SemanticError, SyntaxError
from minidb.lexer import Lexer
from minidb.parser import Parser
from minidb.plan import PlanBuilder
from minidb.semantic import SemanticAnalyzer
from minidb.tokens import TokenType


def analyzer_with_student():
    catalog = object.__new__(PagedCatalog)
    catalog.tables = {
        "student": TableSchema("student", [ColumnSchema("id", "INT"), ColumnSchema("name", "VARCHAR", 5)])
    }
    return SemanticAnalyzer(catalog)


class LexerAcceptanceTests(unittest.TestCase):
    def test_all_required_token_categories_and_locations(self):
        tokens = Lexer("SELECT name FROM t WHERE id>=12;").tokenize()
        self.assertEqual(
            [token.type for token in tokens[:-1]],
            [TokenType.KEYWORD, TokenType.IDENTIFIER, TokenType.KEYWORD, TokenType.IDENTIFIER,
             TokenType.KEYWORD, TokenType.IDENTIFIER, TokenType.OPERATOR, TokenType.INTEGER,
             TokenType.DELIMITER],
        )
        self.assertEqual((tokens[5].lexeme, tokens[5].location.line, tokens[5].location.column), ("id", 1, 26))

    def test_comments_case_and_escaped_string(self):
        tokens = Lexer("/* first\nsecond */ InSeRt INTO t(name) VALUES('O''Neil');").tokenize()
        self.assertEqual(tokens[0].lexeme, "INSERT")
        self.assertEqual((tokens[0].location.line, tokens[0].location.column), (2, 11))
        self.assertEqual(next(token.value for token in tokens if token.type is TokenType.STRING), "O'Neil")

    def test_lexical_failures_are_classified_and_located(self):
        cases = ["SELECT @;", "SELECT 12abc FROM t;", "SELECT 'open FROM t;", "/* open"]
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(LexicalError) as caught:
                Lexer(sql).tokenize()
            self.assertIsNotNone(caught.exception.location)


class ParserAcceptanceTests(unittest.TestCase):
    def test_required_statements_build_distinct_ast_nodes(self):
        nodes = Parser(
            "CREATE TABLE t(id INT);"
            "INSERT INTO t(id) VALUES(1);"
            "SELECT * FROM t WHERE NOT(id<1);"
            "DELETE FROM t WHERE id=1;"
        ).parse_all()
        self.assertEqual([type(node) for node in nodes], [CreateTableStmt, InsertStmt, SelectStmt, DeleteStmt])

    def test_two_bounded_extensions_parse(self):
        update, ordered = Parser("UPDATE t SET id=id+1; SELECT id FROM t ORDER BY id DESC;").parse_all()
        self.assertIsInstance(update, UpdateStmt)
        self.assertEqual(ordered.order_by[0].direction, "DESC")

    def test_syntax_error_reports_expected_symbol_and_position(self):
        with self.assertRaises(SyntaxError) as caught:
            Parser("SELECT id t;").parse_all()
        self.assertEqual(caught.exception.expected, ["FROM"])
        self.assertEqual((caught.exception.location.line, caught.exception.location.column), (1, 11))


class SemanticAndPlanAcceptanceTests(unittest.TestCase):
    def test_table_column_count_duplicate_and_type_rules(self):
        analyzer = analyzer_with_student()
        invalid = [
            "SELECT id FROM missing;",
            "SELECT absent FROM student;",
            "INSERT INTO student(id,name) VALUES(1);",
            "INSERT INTO student(id,id) VALUES(1,2);",
            "INSERT INTO student(id,name) VALUES('wrong','ok');",
        ]
        for sql in invalid:
            with self.subTest(sql=sql), self.assertRaises(SemanticError):
                analyzer.analyze(Parser(sql).parse_all()[0])

    def test_varchar_and_boolean_expression_rules(self):
        analyzer = analyzer_with_student()
        for sql in ("INSERT INTO student(id,name) VALUES(1,'longer');", "SELECT * FROM student WHERE id+1;"):
            with self.subTest(sql=sql), self.assertRaises(SemanticError):
                analyzer.analyze(Parser(sql).parse_all()[0])

    def test_base_select_plan_has_project_filter_and_scan(self):
        statement = Parser("SELECT name FROM student WHERE id>=1;").parse_all()[0]
        analyzer_with_student().analyze(statement)
        plan = PlanBuilder().build(statement)
        self.assertEqual((plan.kind, plan.children[0].kind, plan.children[0].children[0].kind),
                         ("Project", "Filter", "SeqScan"))

    def test_inspect_reports_all_statements_without_mutating_catalog(self):
        from minidb import Database
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            result = db.inspect("CREATE TABLE staged(id INT); INSERT INTO staged(id) VALUES(1); SELECT id FROM staged;")
            self.assertEqual([item["tokens"][0]["lexeme"] for item in result], ["CREATE", "INSERT", "SELECT"])
            self.assertFalse(db.catalog.exists("staged"))
            db.close()

    def test_optimizer_removes_tautology_from_filter(self):
        from minidb import Database
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            db = Database(directory)
            db.execute("CREATE TABLE student(id INT,name VARCHAR(5));")
            info = db.inspect("SELECT name FROM student WHERE 1=1 AND id>=1;")[0]
            self.assertIn("1 = 1", info["plan_before"])
            self.assertNotIn("1 = 1", info["plan_after"])
            db.close()


if __name__ == "__main__":
    unittest.main()

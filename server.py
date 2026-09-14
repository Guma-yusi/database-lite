from __future__ import annotations

import json
import mimetypes
import shutil
import sqlite3
import sys
from datetime import date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse
from minidb import Database as MiniDatabase
from minidb.errors import MiniDBError
from os_sim import QueryScheduler, StorageService
from os_sim.cache import PageCache
from os_sim.page_file import PAGE_SIZE, PAYLOAD_SIZE

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "library.db"
STATIC = ROOT / "static"
OS_STORE_PATH = ROOT / "os_sim_demo"
CACHE_POLICIES = list(PageCache.SUPPORTED_POLICIES)
MINIDB = MiniDatabase(ROOT / "minidb_demo", buffer_pages=8)
OS_STORE = StorageService(OS_STORE_PATH, cache_pages=8, policy="LRU", dirty_ratio=.75, background_interval=2.0)
OS_STORE_LOCK = Lock()
if "student" not in MINIDB.catalog.tables:
    MINIDB.execute("CREATE TABLE student(id INT,name VARCHAR(20),age INT); INSERT INTO student(id,name,age) VALUES(1,'Alice',20); INSERT INTO student(id,name,age) VALUES(2,'Bob',17);")


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        result = super().__exit__(exc_type, exc, tb)
        self.close()
        return result


def connect():
    db = sqlite3.connect(DB_PATH, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db(reset=False):
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with connect() as db:
        db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        db.executescript((ROOT / "seed.sql").read_text(encoding="utf-8"))


def rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def os_status():
    with OS_STORE_LOCK:
        return {
            "stats": OS_STORE.stats(),
            "pages": OS_STORE.page_directory(),
            "frames": [
                {
                    "page_id": f.page_id,
                    "generation": f.generation,
                    "dirty": f.dirty,
                    "pin_count": f.pin_count,
                    "page_lsn": f.page_lsn,
                    "hits": f.hits,
                    "ref_bit": f.ref_bit,
                }
                for f in OS_STORE.cache.frames.values()
            ],
            "events": OS_STORE.cache.recent_events(30),
            "policies": CACHE_POLICIES,
        }


def os_page_detail(page_id):
    with OS_STORE_LOCK:
        control = OS_STORE.file.control
        frame = OS_STORE.cache.frames.get(page_id)
        allocated = 0 <= page_id < len(control["allocated"]) and control["allocated"][page_id]
        detail = {
            "page_id": page_id,
            "page_size": PAGE_SIZE,
            "allocated": allocated,
            "cached": frame is not None,
            "generation": frame.generation if frame else (control["page_generations"][page_id] if 0 <= page_id < len(control["page_generations"]) else 0),
            "checksum": control["checksums"][page_id] if 0 <= page_id < len(control["checksums"]) else 0,
            "offset": page_id * PAGE_SIZE,
        }
        if not allocated:
            detail.update({"preview_text": "", "hex_rows": [], "ascii_rows": []})
            return detail
        payload = frame.payload if frame else OS_STORE.file._read_raw(page_id)
        preview = payload[:128]
        hex_rows, ascii_rows = [], []
        for start in range(0, min(len(preview), 64), 16):
            chunk = preview[start:start + 16]
            hex_rows.append(" ".join(f"{byte:02X}" for byte in chunk))
            ascii_rows.append("".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk))
        detail.update({
            "preview_text": preview.rstrip(b"\x00").decode("utf-8", errors="replace"),
            "hex_rows": hex_rows,
            "ascii_rows": ascii_rows,
        })
        return detail


def rebuild_os_store(policy="LRU", reset=False):
    global OS_STORE
    if policy not in CACHE_POLICIES:
        raise ApiError(400, "不支持的缓存策略")
    with OS_STORE_LOCK:
        OS_STORE.close(clean=not reset)
        if reset:
            shutil.rmtree(OS_STORE_PATH, ignore_errors=True)
        else:
            (OS_STORE_PATH / "cache_events.ndjson").unlink(missing_ok=True)
        OS_STORE = StorageService(OS_STORE_PATH, cache_pages=8, policy=policy, dirty_ratio=.75, background_interval=2.0)
    return os_status()


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise ApiError(400, "请求数据不是有效的 JSON")

    def route(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        path, query = self.route()
        try:
            if path.startswith("/api/"):
                return self.get_api(path, query)
            target = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            if not target.resolve().is_relative_to(STATIC.resolve()) or not target.is_file():
                return self.send_error(404)
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:
            self.send_json({"error": f"服务器错误：{exc}"}, 500)

    def get_api(self, path, query):
        q = query.get("q", [""])[0].strip()
        with connect() as db:
            if path == "/api/dashboard":
                stats = dict(db.execute("""SELECT
                  (SELECT SUM(total_copies) FROM books) books,
                  (SELECT SUM(available_copies) FROM books) available,
                  (SELECT COUNT(*) FROM readers) readers,
                  (SELECT COUNT(*) FROM loans WHERE status='借阅中') active
                """).fetchone())
                recent = rows(db.execute("""SELECT l.*,b.title,b.shelf_code,r.name,r.card_no,
                  CASE WHEN l.status='借阅中' AND date(l.due_at)<date('now','localtime') THEN '已逾期' ELSE l.status END display_status
                  FROM loans l JOIN books b ON b.id=l.book_id JOIN readers r ON r.id=l.reader_id
                  ORDER BY l.id DESC LIMIT 12"""))
                return self.send_json({"stats": stats, "loans": recent})
            if path == "/api/books":
                return self.send_json(rows(db.execute("SELECT * FROM books WHERE title LIKE ? OR author LIKE ? OR isbn LIKE ? ORDER BY id DESC", (f"%{q}%",)*3)))
            if path == "/api/readers":
                return self.send_json(rows(db.execute("SELECT * FROM readers WHERE name LIKE ? OR card_no LIKE ? OR department LIKE ? ORDER BY id DESC", (f"%{q}%",)*3)))
            if path == "/api/loans":
                return self.send_json(rows(db.execute("""SELECT l.*,b.title,b.shelf_code,r.name,r.card_no,
                  CASE WHEN l.status='借阅中' AND date(l.due_at)<date('now','localtime') THEN '已逾期' ELSE l.status END display_status
                  FROM loans l JOIN books b ON b.id=l.book_id JOIN readers r ON r.id=l.reader_id
                  WHERE b.title LIKE ? OR r.name LIKE ? OR l.loan_no LIKE ? ORDER BY l.id DESC""", (f"%{q}%",)*3)))
            if path == "/api/minidb/status":
                return self.send_json({"catalog":MINIDB.catalog.rows(),"pages":MINIDB.store.page_directory(),"buffer":MINIDB.stats()})
            if path == "/api/os/status":
                return self.send_json(os_status())
            if path == "/api/os/page":
                try:
                    page_id = int(query.get("page_id", ["0"])[0])
                except ValueError:
                    raise ApiError(400, "page_id 必须是整数")
                return self.send_json(os_page_detail(page_id))
        raise ApiError(404, "接口不存在")

    def do_POST(self):
        path, _ = self.route()
        try:
            data = self.read_json()
            with connect() as db:
                if path == "/api/books":
                    required(data, "isbn", "title", "author")
                    total = int(data.get("total_copies", 1))
                    cur = db.execute("INSERT INTO books(isbn,title,author,category,publisher,total_copies,available_copies,shelf_code) VALUES(?,?,?,?,?,?,?,?)",
                        (data["isbn"], data["title"], data["author"], data.get("category","其他"), data.get("publisher",""), total, total, data.get("shelf_code","")))
                    return self.send_json({"id": cur.lastrowid, "message": "图书已入库"}, 201)
                if path == "/api/readers":
                    required(data, "card_no", "name")
                    cur = db.execute("INSERT INTO readers(card_no,name,phone,department,status) VALUES(?,?,?,?,?)",
                        (data["card_no"], data["name"], data.get("phone",""), data.get("department",""), data.get("status","正常")))
                    return self.send_json({"id": cur.lastrowid, "message": "读者已登记"}, 201)
                if path == "/api/loans":
                    required(data, "book_id", "reader_id")
                    book = db.execute("SELECT * FROM books WHERE id=?", (data["book_id"],)).fetchone()
                    reader = db.execute("SELECT * FROM readers WHERE id=?", (data["reader_id"],)).fetchone()
                    if not book or book["available_copies"] < 1: raise ApiError(409, "该图书当前无可借副本")
                    if not reader or reader["status"] != "正常": raise ApiError(409, "读者不存在或已停用")
                    today = date.today(); due = today + timedelta(days=int(data.get("days", 14)))
                    loan_no = f"JY{today:%Y%m%d}{db.execute('SELECT COUNT(*)+1 FROM loans').fetchone()[0]:03d}"
                    db.execute("UPDATE books SET available_copies=available_copies-1 WHERE id=?", (book["id"],))
                    cur = db.execute("INSERT INTO loans(loan_no,book_id,reader_id,borrowed_at,due_at,status) VALUES(?,?,?,?,?,'借阅中')",
                        (loan_no, book["id"], reader["id"], today.isoformat(), due.isoformat()))
                    return self.send_json({"id": cur.lastrowid, "loan_no": loan_no, "message": "借阅办理成功"}, 201)
                if path in ("/api/minidb/inspect", "/api/minidb/execute"):
                    sql=data.get("sql","")
                    if not sql.strip(): raise ApiError(400,"请输入 SQL")
                    try:
                        result=MINIDB.inspect(sql) if path.endswith("inspect") else MINIDB.execute(sql)
                        allocated=[page for page in MINIDB.store.page_directory() if page["allocated"]]
                        return self.send_json({"result":result,"buffer":MINIDB.stats(),"system":{"catalog":MINIDB.catalog.rows(),"allocated_pages":allocated}})
                    except MiniDBError as exc: raise ApiError(400,str(exc))
                if path == "/api/os/action":
                    action=data.get("action")
                    if action=="allocate":result={"page_id":OS_STORE.allocate_page()}
                    elif action=="write":
                        pid=int(data["page_id"]);raw=str(data.get("text","页面数据")).encode("utf-8")
                        if len(raw)>PAYLOAD_SIZE:raise ApiError(400,"内容超过一页容量")
                        result={"lsn":OS_STORE.write_page(pid,raw+bytes(PAYLOAD_SIZE-len(raw)))}
                    elif action=="checkpoint":result=OS_STORE.checkpoint()
                    elif action=="set_policy":
                        policy=str(data.get("policy","")).upper()
                        return self.send_json({"message":f"缓存策略已切换为 {policy}","status":rebuild_os_store(policy=policy, reset=False)})
                    elif action=="reset":
                        policy=str(data.get("policy", OS_STORE.cache.policy)).upper()
                        return self.send_json({"message":"OS 仿真台已重置","status":rebuild_os_store(policy=policy, reset=True)})
                    elif action=="workload":
                        allocated=[p["page_id"] for p in OS_STORE.page_directory() if p["allocated"]]
                        if not allocated:allocated=[OS_STORE.allocate_page()]
                        scheduler=QueryScheduler(workers=8,queue_capacity=64)
                        futures=[scheduler.submit(OS_STORE.read_page,allocated[i%len(allocated)]) for i in range(1000)]
                        [f.result() for f in futures];result=scheduler.stats();scheduler.close()
                    else:raise ApiError(400,"未知 OS 仿真动作")
                    return self.send_json({"result":result,"status":OS_STORE.stats()})
            raise ApiError(404, "接口不存在")
        except ApiError as exc: self.send_json({"error": exc.message}, exc.status)
        except sqlite3.IntegrityError as exc: self.send_json({"error": "编号已存在或数据不符合约束", "detail": str(exc)}, 409)
        except Exception as exc: self.send_json({"error": f"服务器错误：{exc}"}, 500)

    def do_PUT(self):
        path, _ = self.route()
        try:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "loans"] and parts[3] == "return":
                loan_id = int(parts[2])
                with connect() as db:
                    loan = db.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
                    if not loan: raise ApiError(404, "借阅记录不存在")
                    if loan["status"] == "已归还": raise ApiError(409, "该图书已经归还")
                    db.execute("UPDATE loans SET status='已归还',returned_at=? WHERE id=?", (date.today().isoformat(), loan_id))
                    db.execute("UPDATE books SET available_copies=available_copies+1 WHERE id=?", (loan["book_id"],))
                return self.send_json({"message": "归还成功"})
            raise ApiError(404, "接口不存在")
        except ApiError as exc: self.send_json({"error": exc.message}, exc.status)
        except Exception as exc: self.send_json({"error": f"服务器错误：{exc}"}, 500)


def required(data, *keys):
    missing = [k for k in keys if data.get(k) in (None, "")]
    if missing: raise ApiError(400, "请填写必填字段：" + "、".join(missing))


if __name__ == "__main__":
    reset = "--reset" in sys.argv
    init_db(reset)
    port = 8000
    print(f"拾页图书室已启动：http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止服务")
    httpd=ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:httpd.serve_forever()
    except KeyboardInterrupt:print("\n服务已停止，正在写回脏页…")
    finally:
        httpd.server_close();MINIDB.close();OS_STORE.close()

import operator
from threading import Condition
from .ast import BinaryExpr,IdentifierExpr,LiteralExpr,UnaryExpr
from .catalog import ColumnSchema,TableSchema
from .storage.page import NO_PAGE
from .errors import StorageError


OPS={"+":operator.add,"-":operator.sub,"*":operator.mul,"/":operator.truediv,
     "=":operator.eq,"==":operator.eq,"!=":operator.ne,">":operator.gt,">=":operator.ge,"<":operator.lt,"<=":operator.le,
     "AND":lambda a,b:bool(a and b),"OR":lambda a,b:bool(a or b)}


def evaluate(expr,row):
    if isinstance(expr,LiteralExpr):return expr.value
    if isinstance(expr,IdentifierExpr):return next((v for k,v in row.items() if k.lower()==expr.name.lower()),None)
    if isinstance(expr,UnaryExpr):return not evaluate(expr.operand,row)
    if isinstance(expr,BinaryExpr):return OPS[expr.operator](evaluate(expr.left,row),evaluate(expr.right,row))
    return True


class RWLock:
    """表级读写锁：多个 SELECT 可并行，写操作独占。"""
    def __init__(self):self.cv=Condition();self.readers=0;self.writer=False
    def acquire_read(self):
        with self.cv:
            while self.writer:self.cv.wait()
            self.readers+=1
    def release_read(self):
        with self.cv:self.readers-=1;self.cv.notify_all()
    def acquire_write(self):
        with self.cv:
            while self.writer or self.readers:self.cv.wait()
            self.writer=True
    def release_write(self):
        with self.cv:self.writer=False;self.cv.notify_all()


class TableHeap:
    def __init__(self,catalog,buffer,codec):self.catalog,self.buffer,self.codec=catalog,buffer,codec
    def _pages(self,schema):
        pid=schema.first_page
        while pid is not None and pid!=NO_PAGE:
            yield pid
            with self.buffer.page(pid) as page:pid=page.next_page
    def scan(self,schema):
        pid=schema.first_page
        while pid is not None and pid!=NO_PAGE:
            with self.buffer.page(pid) as page:
                next_pid=page.next_page
                if next_pid!=NO_PAGE:self.buffer.prefetch(next_pid)
                for slot,payload in page.records():yield (pid,slot),self.codec.decode(schema,payload)
            pid=next_pid
    def insert(self,schema,row):
        payload=self.codec.encode(schema,row);last=None
        for pid in self._pages(schema):
            last=pid
            with self.buffer.page(pid) as page:
                slot=page.insert(payload)
                if slot is not None:return pid,slot
        page=self.buffer.new_table_page(schema.name);pid=page.page_id;slot=page.insert(payload)
        if slot is None:
            self.buffer.discard_page(pid)
            raise StorageError("record is larger than one page")
        self.buffer.unpin(pid,True)
        if schema.first_page is None:schema.first_page=pid;self.catalog.save()
        elif last is not None:
            with self.buffer.page(last) as previous:previous.next_page=pid
        return pid,slot
    def delete(self,rid):
        with self.buffer.page(rid[0]) as page:return page.delete(rid[1])
    def update(self,schema,rid,row):
        payload=self.codec.encode(schema,row)
        with self.buffer.page(rid[0]) as page:
            if page.update(rid[1],payload):return rid
        new_rid=self.insert(schema,row)
        if not self.delete(rid):raise StorageError(f"record {rid} disappeared during update")
        return new_rid


class Executor:
    def __init__(self,catalog,buffer,codec):self.catalog=catalog;self.heap=TableHeap(catalog,buffer,codec);self.locks={}
    def lock(self,table):return self.locks.setdefault(table.lower(),RWLock())
    def run(self,plan):
        kind=plan.kind
        if kind=="CreateTable":
            schema=TableSchema(plan.args["table"],[ColumnSchema(n,t,l) for n,t,l in plan.args["columns"]]);self.catalog.create_table(schema);return {"message":f"table '{schema.name}' created","affected":0}
        if kind=="Insert":
            schema=self.catalog.table(plan.args["table"]);row={c.name:None for c in schema.columns}
            for name,value in zip(plan.args["columns"],plan.args["values"]):row[next(c.name for c in schema.columns if c.name.lower()==name.lower())]=evaluate(value,{})
            lock=self.lock(schema.name);lock.acquire_write()
            try:rid=self.heap.insert(schema,row)
            finally:lock.release_write()
            return {"message":"1 row inserted","affected":1,"rid":{"page":rid[0],"slot":rid[1]}}
        if kind=="Delete":
            schema=self.catalog.table(plan.args["table"]);lock=self.lock(schema.name);lock.acquire_write();affected=0
            try:
                for rid,row in list(self._rows(plan.children[0])):
                    if self.heap.delete(rid):affected+=1
            finally:lock.release_write()
            return {"message":f"{affected} row(s) deleted","affected":affected}
        if kind=="Update":
            schema=self.catalog.table(plan.args["table"]);lock=self.lock(schema.name);lock.acquire_write();affected=0
            try:
                for rid,row in list(self._rows(plan.children[0])):
                    original=dict(row);updated=dict(row)
                    for name,value in plan.args["assignments"]:
                        actual=next(c.name for c in schema.columns if c.name.lower()==name.lower())
                        updated[actual]=evaluate(value,original)
                    self.heap.update(schema,rid,updated);affected+=1
            finally:lock.release_write()
            return {"message":f"{affected} row(s) updated","affected":affected}
        table_node=plan
        while table_node.children:table_node=table_node.children[0]
        lock=self.lock(table_node.args["table"]);lock.acquire_read()
        try:rows=[row for _,row in self._rows(plan)]
        finally:lock.release_read()
        return {"columns":list(rows[0]) if rows else self._columns(plan),"rows":rows,"affected":len(rows)}
    def _columns(self,plan):
        if plan.kind=="Project":return plan.args["columns"]
        node=plan
        while node.children:node=node.children[0]
        return [c.name for c in self.catalog.table(node.args["table"]).columns]
    def _rows(self,plan):
        if plan.kind=="SeqScan":
            schema=self.catalog.table(plan.args["table"])
            if schema.name.lower()=="pg_catalog":
                for slot,row in enumerate(self.catalog.rows()):
                    yield (0,slot),row
                return
            yield from self.heap.scan(schema)
        elif plan.kind=="Filter":
            for rid,row in self._rows(plan.children[0]):
                if evaluate(plan.args["predicate"],row):yield rid,row
        elif plan.kind=="Project":
            for rid,row in self._rows(plan.children[0]):yield rid,{c:next(v for k,v in row.items() if k.lower()==c.lower()) for c in plan.args["columns"]}
        elif plan.kind=="Sort":
            rows=list(self._rows(plan.children[0]))
            for column,direction in reversed(plan.args["keys"]):
                present=[];nulls=[]
                for item in rows:
                    value=next(v for k,v in item[1].items() if k.lower()==column.lower())
                    (nulls if value is None else present).append(item)
                present.sort(key=lambda item:next(v for k,v in item[1].items() if k.lower()==column.lower()),reverse=direction=="DESC")
                rows=present+nulls
            yield from rows

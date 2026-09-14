# 前端页面功能（OS 存储仿真台）：数据与动作主入口；server.py 的 OS_STORE 即其实例。
# 页目录 / Buffer Frames / 缓存事件 / 指标均来自它的 stats、page_directory、cache，
# 按钮“分配页/修改内存页/Checkpoint/重置”都由它执行。
"""
统一页服务门面模块。

功能：
- 组合 PageFile、WAL 和 PageCache，对数据库模块暴露统一的页级访问接口。
- 负责读页、写页、分配页、释放页、表页映射、预读、刷脏页、checkpoint、恢复与后台脏页写回。
- 是数据库模块对接操作系统子系统时使用的主入口。

对应需求文档：
- 第 3 节“总体架构（建议）”中的 Storage API。
- 第 6.1 节“必备 API 列表”中的页式存储主接口。
- 第 7.1 节“数据库如何使用本子系统（对接约束）”。
- 第 7.2 节“必须满足的集成要求”。
- 第 8.3 节“持久化验证（必做）”中的重启恢复能力。
"""

from pathlib import Path
from threading import Event,Thread
from .cache import PageCache
from .errors import CacheAllPinned, InvalidPage, InvalidPageDataSize
from .page_file import EXTENT_PAGES,PAYLOAD_SIZE,PageFile
from .table_map import TablePageMap
from .wal import WriteAheadLog


class StorageService:
    """独立 OS 仿真实体：统一暴露页服务，内部组合文件、WAL 与内存缓存。"""
    def __init__(self,directory,cache_pages=16,policy="LRU",dirty_ratio=0.6,background_interval=None):
        if dirty_ratio<=0:raise ValueError("dirty_ratio must be positive")
        self.root=Path(directory);self.file=PageFile(self.root);self.wal=WriteAheadLog(self.root/"redo.wal")
        self.table_map=TablePageMap(self.root/"table_map.json")
        self.cache=PageCache(self.file,self.wal,cache_pages,policy,event_path=self.root/"cache_events.ndjson");self.dirty_ratio=dirty_ratio;self.recovered_pages=self._recover()
        self.stop_event=Event();self.writer=None
        if background_interval:self.start_background_writer(background_interval)
    def _recover(self):
        recovered=0
        for record in self.wal.records():
            if record["lsn"]<=self.file.checkpoint_lsn:continue
            import base64
            payload=base64.b64decode(record["payload"])
            if len(payload)!=PAYLOAD_SIZE:continue  # v1 WAL 的 4076B after-image 已由页文件迁移处理
            try:_,generation=self.file.read_page(record["page_id"])
            except InvalidPage:continue
            if generation<record["generation"]:
                self.file.write_page(record["page_id"],payload,record["generation"]);recovered+=1
        if recovered:self.file.set_checkpoint(self.wal.durable_lsn)
        return recovered
    def allocate_page(self):return self.file.allocate_page()
    def append_table_page(self,table_name):return self.table_map.append_page(table_name,self.allocate_page)
    def get_table_pages(self,table_name):return self.table_map.get_table_pages(table_name)
    def register_table_page(self,table_name,page_id):self.table_map.register_page(table_name,page_id)
    def free_page(self,page_id):self.release_page(page_id)
    def release_page(self,page_id):
        frame=self.cache.frames.get(page_id)
        if frame and frame.pin_count:raise CacheAllPinned("cannot release a pinned page")
        self.cache.frames.pop(page_id,None);self.file.release_page(page_id);self.table_map.unregister_page(page_id)
    def get_page(self,page_id):return self.read_page(page_id)
    def prefetch_page(self,page_id):return self.cache.prefetch(page_id)
    def read_page(self,page_id):
        frame=self.cache.fetch(page_id)
        try:return frame.payload
        finally:self.cache.unpin(page_id)
    def write_page(self,page_id,payload):
        if len(payload)!=PAYLOAD_SIZE:raise InvalidPageDataSize(f"payload must be exactly {PAYLOAD_SIZE} bytes")
        lsn=self.cache.update(page_id,payload)
        if self.cache.capacity and self.cache.stats()["dirty"]/self.cache.capacity>=self.dirty_ratio:self.flush_due()
        return lsn
    def flush_page(self,page_id):self.cache.flush_page(page_id)
    def flush_all(self):return self.checkpoint()
    def flush_due(self,max_pages=None):
        dirty=[f for f in self.cache.frames.values() if f.dirty and f.pin_count==0]
        dirty.sort(key=lambda f:f.page_lsn)
        for frame in dirty[:max_pages]:self.cache.flush_page(frame.page_id)
        return len(dirty[:max_pages])
    def checkpoint(self):
        self.cache.flush_all();self.file.set_checkpoint(self.wal.durable_lsn)
        return {"checkpoint_lsn":self.file.checkpoint_lsn,"flushed_pages":self.cache.flushes}
    def start_background_writer(self,interval=1.0):
        if self.writer:return
        def loop():
            while not self.stop_event.wait(interval):self.flush_due(max_pages=max(1,self.cache.capacity//4))
        self.writer=Thread(target=loop,name="os-sim-dirty-writer",daemon=True);self.writer.start()
    def stats(self):return {**self.cache.stats(),"wal_durable_lsn":self.wal.durable_lsn,"checkpoint_lsn":self.file.checkpoint_lsn,"recovered_pages":self.recovered_pages,"allocated_pages":sum(self.file.control["allocated"]),"total_pages":max(self.file.control["page_count"],EXTENT_PAGES)}
    def page_directory(self):return self.file.page_directory()
    def close(self,clean=True):
        self.stop_event.set()
        if self.writer:self.writer.join(timeout=2)
        if clean:self.checkpoint()

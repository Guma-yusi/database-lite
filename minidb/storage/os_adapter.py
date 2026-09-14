from contextlib import contextmanager
from .page import SlottedPage


class OSBufferAdapter:
    """把数据库 SlottedPage 接到独立 StorageService，执行器不再直接访问文件。"""
    def __init__(self,store):self.store=store;self.pending={}
    def prefetch(self,page_id):
        if page_id is not None:self.store.prefetch_page(page_id)
    @contextmanager
    def page(self,page_id):
        page=SlottedPage(page_id,self.store.read_page(page_id))
        try:yield page
        finally:
            if page.dirty:self.store.write_page(page_id,bytes(page.data))
    def new_page(self):
        page_id=self.store.allocate_page();page=SlottedPage(page_id);self.pending[page_id]=page;return page
    def new_table_page(self,table_name):
        page_id=self.store.append_table_page(table_name);page=SlottedPage(page_id);self.pending[page_id]=page;return page
    def unpin(self,page_id,dirty=False):
        page=self.pending.pop(page_id,None)
        if page is not None and (dirty or page.dirty):self.store.write_page(page_id,bytes(page.data))
    def discard_page(self,page_id):
        self.pending.pop(page_id,None);self.store.release_page(page_id)
    def flush_all(self):self.store.checkpoint()
    def stats(self):return self.store.stats()

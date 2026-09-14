# 前端页面功能（OS 存储仿真台）：对应“Buffer Frames”表格、“缓存事件”面板，
# 以及指标区的“缓存策略/命中率/脏页”，由 OS_STORE.cache.frames 与 recent_events 读取。
"""
页缓存与替换策略模块。

功能：
- 实现 Buffer Pool / Page Cache，缓存磁盘页并支持命中统计。
- 支持 FIFO/LRU/LFU/CLOCK 四种替换策略，并支持顺序扫描预读。
- 负责脏页刷盘、淘汰、pin_count 保护，以及缓存事件日志记录。

对应需求文档：
- 第 3 节“总体架构（建议）”中的 BufferPool/Cache。
- 第 5.1 节“缓存基本能力（必做）”。
- 第 5.2 节“替换策略（必做）”。
- 第 5.3 节“命中统计与替换日志（必做）”。
- 第 7.2 节“必须满足的集成要求”中的缓存命中统计与页替换日志输出。
"""

from collections import OrderedDict
from dataclasses import dataclass
import json
from pathlib import Path
from threading import RLock
from time import monotonic
from .errors import CacheAllPinned, IoError, WriteBarrierError


@dataclass
class Frame:
    page_id:int; payload:bytes; generation:int; dirty:bool=False; pin_count:int=0; page_lsn:int=0; loaded_at:float=0; hits:int=1; ref_bit:int=1; inserted_seq:int=0


class PageCache:
    """支持 FIFO/LRU/LFU/CLOCK 的页缓存；缓存数据页，目录和 WAL 由独立持久化路径管理。"""
    SUPPORTED_POLICIES=("FIFO","LFU","LRU","CLOCK")
    def __init__(self,page_file,wal,capacity=16,policy="LRU",event_limit=2000,event_path=None):
        if policy not in self.SUPPORTED_POLICIES:raise ValueError(f"policy must be one of {', '.join(self.SUPPORTED_POLICIES)}")
        if capacity<=0:raise ValueError("capacity must be a positive integer")
        if event_limit<=0:raise ValueError("event_limit must be a positive integer")
        self.file,self.wal,self.capacity,self.policy=page_file,wal,capacity,policy;self.frames=OrderedDict();self.lock=RLock()
        self.hits=self.misses=self.evictions=self.flushes=0;self.events=[];self.event_limit=event_limit;self.sequence=0
        self.insert_sequence=0;self.clock_hand=0
        self.event_path=Path(event_path) if event_path else None
    def _next_insert_sequence(self):
        self.insert_sequence+=1
        return self.insert_sequence
    def _record_hit(self,page_id,frame):
        frame.hits+=1;frame.ref_bit=1
        if self.policy=="LRU":self.frames.move_to_end(page_id)
        self._log("hit",page_id,hits=frame.hits)
    def _new_frame(self,page_id,payload,generation):
        return Frame(page_id,payload,generation,loaded_at=monotonic(),hits=1,ref_bit=1,inserted_seq=self._next_insert_sequence())
    def _select_victim(self):
        if self.policy in ("FIFO","LRU"):
            for idx,(page_id,frame) in enumerate(self.frames.items()):
                if frame.pin_count==0:return idx,page_id,frame
            raise CacheAllPinned("cache pressure: all frames are pinned")
        if self.policy=="LFU":
            candidates=[(frame.hits,frame.inserted_seq,idx,page_id,frame) for idx,(page_id,frame) in enumerate(self.frames.items()) if frame.pin_count==0]
            if not candidates:raise CacheAllPinned("cache pressure: all frames are pinned")
            _,_,idx,page_id,frame=min(candidates)
            return idx,page_id,frame
        unpinned_exists=any(frame.pin_count==0 for frame in self.frames.values())
        if not unpinned_exists:raise CacheAllPinned("cache pressure: all frames are pinned")
        while True:
            page_ids=list(self.frames.keys())
            idx=self.clock_hand%len(page_ids);page_id=page_ids[idx];frame=self.frames[page_id]
            self.clock_hand=(idx+1)%len(page_ids)
            if frame.pin_count>0:continue
            if frame.ref_bit:
                frame.ref_bit=0;self._log("clock-scan",page_id,hits=frame.hits)
                continue
            return idx,page_id,frame
    def _log(self,event,page_id,**extra):
        self.sequence+=1
        entry={"sequence":self.sequence,"time":round(monotonic(),6),"event":event,"page_id":page_id,**extra}
        self.events.append(entry);self.events=self.events[-self.event_limit:]
        if self.event_path:
            try:
                self.event_path.parent.mkdir(parents=True,exist_ok=True)
                with self.event_path.open("ab") as handle:
                    handle.write((json.dumps(entry,ensure_ascii=False)+"\n").encode("utf-8"))
            except OSError as exc:
                raise IoError(f"failed to append cache event log: {self.event_path}") from exc
    def recent_events(self,limit=30):
        if not self.event_path or not self.event_path.exists():
            return self.events[-limit:]
        try:
            lines=self.event_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise IoError(f"failed to read cache event log: {self.event_path}") from exc
        events=[]
        for line in lines[-limit:]:
            try:events.append(json.loads(line))
            except ValueError:continue
        return events
    def fetch(self,page_id,pin=True):
        with self.lock:
            if page_id in self.frames:
                self.hits+=1;frame=self.frames[page_id];self._record_hit(page_id,frame)
            else:
                self.misses+=1
                if len(self.frames)>=self.capacity:self._evict(page_id)
                payload,generation=self.file.read_page(page_id);frame=self._new_frame(page_id,payload,generation);self.frames[page_id]=frame;self._log("miss",page_id)
            if pin:frame.pin_count+=1
            return frame
    def prefetch(self,page_id):
        with self.lock:
            if page_id in self.frames:
                frame=self.frames[page_id];frame.ref_bit=1;self._log("prefetch-skip",page_id,hits=frame.hits)
                return False
            if len(self.frames)>=self.capacity:self._evict(page_id)
            payload,generation=self.file.read_page(page_id);frame=self._new_frame(page_id,payload,generation);self.frames[page_id]=frame;self._log("prefetch",page_id)
            return True
    def unpin(self,page_id):
        with self.lock:self.frames[page_id].pin_count=max(0,self.frames[page_id].pin_count-1)
    def update(self,page_id,payload):
        with self.lock:
            frame=self.fetch(page_id);generation=frame.generation+1;lsn=self.wal.append_page(page_id,generation,payload)
            frame.payload,frame.generation,frame.dirty,frame.page_lsn=payload,generation,True,lsn;self.unpin(page_id);self._log("dirty",page_id,lsn=lsn);return lsn
    def _flush(self,frame):
        if frame.page_lsn>self.wal.durable_lsn:raise WriteBarrierError(f"WAL {frame.page_lsn} is not durable")
        self.file.write_page(frame.page_id,frame.payload,frame.generation);frame.dirty=False;self.flushes+=1;self._log("flush",frame.page_id,lsn=frame.page_lsn)
    def flush_page(self,page_id):
        with self.lock:
            frame=self.frames.get(page_id)
            if frame and frame.dirty:self._flush(frame)
    def flush_all(self):
        with self.lock:
            for frame in self.frames.values():
                if frame.dirty:self._flush(frame)
    def _evict(self,new_page_id):
        idx,page_id,frame=self._select_victim()
        was_dirty=frame.dirty;flushed=False
        if frame.dirty:self._flush(frame);flushed=True
        del self.frames[page_id];self.evictions+=1
        if self.policy=="CLOCK" and self.frames:self.clock_hand=idx%len(self.frames)
        elif self.policy=="CLOCK":self.clock_hand=0
        self._log("replace",page_id,victim_page_id=page_id,new_page_id=new_page_id,dirty=was_dirty,flushed=flushed,policy=self.policy,hits=frame.hits)
    def stats(self):
        total=self.hits+self.misses
        return {"policy":self.policy,"capacity":self.capacity,"resident":len(self.frames),"dirty":sum(f.dirty for f in self.frames.values()),"hits":self.hits,"misses":self.misses,"hit_rate":self.hits/total if total else 0.0,"evictions":self.evictions,"flushes":self.flushes,"disk_reads":self.file.reads,"disk_writes":self.file.writes}

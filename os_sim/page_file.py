# 前端页面功能（OS 存储仿真台）：对应“页目录”格子图（page_directory 的分配状态、
# 偏移与 generation）与指标区的“磁盘读取”，是磁盘页的物理读写来源。
"""
页文件与页目录控制模块。

功能：
- 管理 4KB 固定大小页面在 `tablespace.dat` 中的物理布局。
- 负责页分配、释放、读写、extent 扩容、页校验和与 generation 管理。
- 通过双份 control 文件持久化页目录、分配状态和 checkpoint 元数据。

对应需求文档：
- 第 3 节“总体架构（建议）”中的 Disk/PageFile 和 PageManager。
- 第 4.1 节“Page 数据结构（必做）”中的固定页大小与页编号。
- 第 4.2 节“页分配与释放（必做）”。
- 第 4.3 节“页读写（必做）”。
- 第 2.2 节“存储介质（必做）”中的持久化要求。
- 第 8.3 节“持久化验证（必做）”。
"""

from __future__ import annotations

import json
import os
import zlib
from pathlib import Path
from threading import RLock
from time import sleep
from uuid import uuid4
from .errors import CorruptPage, InvalidPage, InvalidPageDataSize, IoError

PAGE_SIZE = 4096
PAYLOAD_SIZE = PAGE_SIZE
EXTENT_PAGES = 64
LEGACY_HEADER_SIZE = 20


class PageFile:
    """完整 4KB 页文件；校验和/generation 存入双份页目录控制文件。"""
    def __init__(self, directory: str | Path):
        self.root = Path(directory); self.root.mkdir(parents=True, exist_ok=True)
        self.data_path = self.root / "tablespace.dat"
        self.control_paths = [self.root / "control.0.json", self.root / "control.1.json"]
        self.lock = RLock(); self.reads = self.writes = self.allocations = self.releases = 0
        if not self.data_path.exists(): self.data_path.write_bytes(b"")
        self._control_needs_save=False;self.control = self._load_control()
        if self._control_needs_save:self._save_control()

    def _default_control(self):
        return {"version":2,"generation":0,"page_count":0,"allocated":[],"page_generations":[],"checksums":[],"checkpoint_lsn":0}

    def _load_control(self):
        copies=[]
        for path in self.control_paths:
            try:
                value=json.loads(path.read_text(encoding="utf-8"))
                if value.get("version") in (1,2):copies.append(value)
            except (OSError,ValueError):pass
        control=max(copies,key=lambda x:x.get("generation",0)) if copies else self._default_control()
        if control.get("version")==1:
            self._migrate_v1_pages(control)
            control["version"]=2;control["page_generations"]=[1 if used else 0 for used in control["allocated"]]
            control["checksums"]=[zlib.crc32(self._read_raw(i)) if used else 0 for i,used in enumerate(control["allocated"])]
            self._control_needs_save=True
        if control["page_count"]*PAGE_SIZE>self.data_path.stat().st_size:raise CorruptPage("control file points beyond tablespace")
        return control

    def _read_raw(self,page_id):
        try:
            with self.data_path.open("rb") as f:
                f.seek(page_id*PAGE_SIZE)
                return f.read(PAGE_SIZE)
        except OSError as exc:
            raise IoError(f"failed to read page {page_id}") from exc

    def _migrate_v1_pages(self,control):
        """v1 把 20B 物理头塞在 4KB frame 内；v2 将头信息移入控制文件。"""
        temp=self.data_path.with_suffix(".migrate.tmp")
        try:
            with self.data_path.open("rb") as source,temp.open("wb") as target:
                for used in control["allocated"]:
                    legacy=source.read(PAGE_SIZE)
                    page=(legacy[LEGACY_HEADER_SIZE:]+bytes(LEGACY_HEADER_SIZE)) if used else bytes(PAGE_SIZE)
                    target.write(page)
                target.flush();os.fsync(target.fileno())
            os.replace(temp,self.data_path)
        except OSError as exc:
            raise IoError(f"failed to migrate legacy pages in {self.data_path}") from exc

    def _save_control(self):
        self.control["generation"]+=1;raw=json.dumps(self.control,ensure_ascii=False,sort_keys=True).encode()
        try:
            for path in self.control_paths:
                temp=path.with_name(path.name+f".{uuid4().hex}.tmp")
                with temp.open("wb") as f:f.write(raw);f.flush();os.fsync(f.fileno())
                for attempt in range(3):
                    try:
                        os.replace(temp,path)
                        break
                    except PermissionError:
                        if attempt==2:raise
                        sleep(.02*(attempt+1))
        except OSError as exc:
            raise IoError(f"failed to persist control files under {self.root}") from exc

    def _grow_extent(self):
        start=self.control["page_count"]
        try:
            with self.data_path.open("ab") as f:f.write(bytes(PAGE_SIZE*EXTENT_PAGES));f.flush();os.fsync(f.fileno())
        except OSError as exc:
            raise IoError(f"failed to grow tablespace {self.data_path}") from exc
        self.control["page_count"]+=EXTENT_PAGES;self.control["allocated"].extend([False]*EXTENT_PAGES)
        self.control["page_generations"].extend([0]*EXTENT_PAGES);self.control["checksums"].extend([0]*EXTENT_PAGES);self._save_control();return start

    def allocate_page(self):
        with self.lock:
            try:page_id=self.control["allocated"].index(False)
            except ValueError:page_id=self._grow_extent()
            self.control["allocated"][page_id]=True;self.control["page_generations"][page_id]=0
            self.control["checksums"][page_id]=zlib.crc32(bytes(PAGE_SIZE));self.allocations+=1;self._save_control();return page_id

    def release_page(self,page_id):
        with self.lock:
            self._validate(page_id);self.control["allocated"][page_id]=False;self.control["page_generations"][page_id]=0;self.control["checksums"][page_id]=0;self.releases+=1
            try:
                with self.data_path.open("r+b") as f:f.seek(page_id*PAGE_SIZE);f.write(bytes(PAGE_SIZE));f.flush();os.fsync(f.fileno())
            except OSError as exc:
                raise IoError(f"failed to release page {page_id}") from exc
            self._save_control()

    def _validate(self,page_id):
        if page_id<0 or page_id>=self.control["page_count"] or not self.control["allocated"][page_id]:raise InvalidPage(f"page {page_id} is not allocated")

    def read_page(self,page_id):
        with self.lock:
            self._validate(page_id)
            payload=self._read_raw(page_id)
            self.reads+=1
            expected=self.control["checksums"][page_id]
            if len(payload)!=PAGE_SIZE or (expected and zlib.crc32(payload)!=expected):raise CorruptPage(f"checksum mismatch on page {page_id}")
            return payload,self.control["page_generations"][page_id]

    def write_page(self,page_id,payload,generation):
        if len(payload)!=PAGE_SIZE:raise InvalidPageDataSize(f"page must be exactly {PAGE_SIZE} bytes")
        with self.lock:
            self._validate(page_id)
            try:
                with self.data_path.open("r+b") as f:f.seek(page_id*PAGE_SIZE);f.write(payload);f.flush();os.fsync(f.fileno())
            except OSError as exc:
                raise IoError(f"failed to write page {page_id}") from exc
            self.control["page_generations"][page_id]=generation;self.control["checksums"][page_id]=zlib.crc32(payload);self.writes+=1;self._save_control()

    def set_checkpoint(self,lsn):self.control["checkpoint_lsn"]=lsn;self._save_control()
    @property
    def checkpoint_lsn(self):return self.control["checkpoint_lsn"]
    def page_directory(self):
        entries = [{"extent":i//EXTENT_PAGES,"page_id":i,"allocated":used,"offset":i*PAGE_SIZE,"generation":self.control["page_generations"][i]} for i,used in enumerate(self.control["allocated"])]
        # 前端「页目录」始终展示一个完整 extent（64 页），重置后未分配也保持可见
        if len(entries) < EXTENT_PAGES:
            entries.extend({"extent":i//EXTENT_PAGES,"page_id":i,"allocated":False,"offset":i*PAGE_SIZE,"generation":0} for i in range(len(entries),EXTENT_PAGES))
        return entries

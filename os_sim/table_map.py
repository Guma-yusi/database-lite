# 前端页面功能（OS 存储仿真台）：不直接在页面渲染，而是支撑数据库侧建表后的
# 表↔页映射；服务层 get_table_pages / append_table_page 依赖它。
"""
表到页集合映射模块。

功能：
- 以持久化 JSON 形式维护 `table_name -> [page_id...]` 的映射关系。
- 为操作系统子系统提供 `get_table_pages`、`append_table_page` 所需的底层状态。
- 在程序重启后恢复映射，满足页式存储子系统的验收口径。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock

from .errors import IoError


class TablePageMap:
    """持久化维护表名到页号列表的映射。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = RLock()
        self.tables: dict[str, list[int]] = {}
        self._load()

    def _normalize(self, table_name: str) -> str:
        return table_name.lower()

    def _load(self) -> None:
        if not self.path.exists():
            self.tables = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IoError(f"failed to load table map: {self.path}") from exc
        self.tables = {
            self._normalize(name): [int(page_id) for page_id in page_ids]
            for name, page_ids in raw.items()
        }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = json.dumps(self.tables, ensure_ascii=False, sort_keys=True).encode("utf-8")
            with tmp.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            raise IoError(f"failed to save table map: {self.path}") from exc

    def get_table_pages(self, table_name: str) -> list[int]:
        with self.lock:
            return list(self.tables.get(self._normalize(table_name), []))

    def register_page(self, table_name: str, page_id: int) -> None:
        key = self._normalize(table_name)
        with self.lock:
            pages = self.tables.setdefault(key, [])
            if page_id not in pages:
                pages.append(page_id)
                self._save()

    def unregister_page(self, page_id: int, table_name: str | None = None) -> None:
        with self.lock:
            changed = False
            if table_name is not None:
                key = self._normalize(table_name)
                pages = self.tables.get(key, [])
                if page_id in pages:
                    pages.remove(page_id)
                    if not pages:
                        self.tables.pop(key, None)
                    changed = True
            else:
                for key in list(self.tables):
                    pages = self.tables[key]
                    if page_id in pages:
                        pages.remove(page_id)
                        if not pages:
                            self.tables.pop(key, None)
                        changed = True
            if changed:
                self._save()

    def append_page(self, table_name: str, allocator) -> int:
        page_id = allocator()
        self.register_page(table_name, page_id)
        return page_id

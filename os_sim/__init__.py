# 前端页面功能（OS 存储仿真台）：后端入口，导出 StorageService（状态展示与动作）
# 与 QueryScheduler（“运行 1000 查询”），支撑 /api/os/status 与 /api/os/action 接口。
"""
面向 MiniDB 的独立操作系统存储仿真实体。
操作系统子系统包入口。

功能：
- 对外导出操作系统子系统的核心能力，包括统一页服务 `StorageService`
  和有界并发查询队列 `QueryScheduler`。

对应需求文档：
- 第 3 节“总体架构（建议）”：作为 Storage API 的统一包入口。
- 第 6 节“对外接口（给数据库模块）需求”：向数据库模块暴露稳定访问入口。
- 第 7 节“与 SQL 编译器/数据库系统的集成点”：作为数据库侧集成 OS 子系统的导出层。
"""

from .service import StorageService
from .scheduler import QueryScheduler
from .table_map import TablePageMap

__all__ = ["StorageService", "QueryScheduler", "TablePageMap"]

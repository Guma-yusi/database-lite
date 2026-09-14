# 拾页 MiniDB

一个把编译原理、操作系统和数据库原理串起来的教学型数据库。SQL 经过 Token、AST、语义检查、逻辑计划和规则优化，再由执行器通过统一页接口访问自研 4KB Page、FIFO/LRU/LFU/CLOCK Cache、顺序扫描 Prefetch、WAL 与二进制表空间。基础范围严格对应课程指导书；每部分最多保留两个容易解释的扩展。MiniDB 内核不使用 SQLite，原图书室页面仅作为独立的上层业务演示保留。

## 快速启动

```powershell
.\start.bat
```

打开 <http://127.0.0.1:8000/?lab=1> 可直接进入“MiniDB 实验台”。Windows 也可双击 `start.bat` 后点击左侧“MiniDB 实验台”。

若系统已配置 Python，也可运行 `py -3 server.py`。命令行数据库入口为 `py -3 minidb_cli.py`。

## 端到端演示

```sql
CREATE TABLE student(id INT, name VARCHAR(20), age INT);
INSERT INTO student(id,name,age) VALUES (1,'Alice',20);
SELECT name FROM student WHERE 1 = 1 AND age > 10 + 8;
UPDATE student SET age = age + 1 WHERE id = 2;
SELECT name,age FROM student ORDER BY age DESC,name ASC;
DELETE FROM student WHERE id = 1;
```

```text
SQL → Lexer/Token → Parser/AST → Semantic/Catalog
    → Logical Plan → Rule Optimizer → Executor
    → TableHeap/RID → OSBufferAdapter → StorageService
    → FIFO/LRU/LFU/CLOCK Cache + Prefetch → WAL → 4KB PageFile
```

## 目录

```text
minidb/
├─ lexer.py tokens.py errors.py       # 词法与精确错误位置
├─ parser.py ast.py                    # 递归下降 Parser 与 AST
├─ semantic.py catalog.py              # 名字绑定、类型检查、页 0 系统目录
├─ plan.py optimizer.py                # Logical Plan 与优化规则
├─ execution.py engine.py              # 执行与流水线门面
└─ storage/
   ├─ page.py                          # 4KB Slotted Page
   ├─ os_adapter.py                    # 数据库行页到 OS 页服务适配器
   ├─ disk.py buffer.py                # 第一阶段独立教学实现（非集成主路径）
   └─ record.py                        # NULL bitmap 与 UTF-8 行编码
os_sim/
├─ page_file.py cache.py wal.py        # 页文件、替换策略与预写日志
├─ service.py interfaces.py            # 数据库唯一调用的存储服务接口
static/                                # 桌面可视化实验台
tests/                                 # 单元、边界、Fuzz、持久化、端到端测试
docs/                                  # 文法、存储、验收矩阵、三人分工与报告
```

第一次运行、课堂演示和答辩前，建议先阅读 [`docs/完整操作演示与源码讲解.md`](docs/完整操作演示与源码讲解.md)。它按“完整操作 → 调用链 → 逐文件职责 → 页/WAL/缓存细节 → 测试证据 → 三人答辩分工”的顺序解释整个系统。

## 测试

```powershell
py -3 -m unittest discover -s tests -v
```

## 独立 OS 存储仿真

```powershell
py -3 os_sim_cli.py allocate
py -3 os_sim_cli.py status
py -3 benchmarks/os_benchmark.py
```

浏览器访问 <http://127.0.0.1:8000/?os=1>，可以观察 Extent 页目录、Buffer Frames、脏页、WAL/Checkpoint LSN、策略切换、页详情十六进制预览和缓存事件。

第二份 PPT 对应资料见 `docs/操作系统模块验收矩阵.md`、`操作系统模块设计取舍.md`、`操作系统模块效果证据.md` 和 `操作系统代码答辩.md`。

第三份指导书对应资料见 `docs/第三阶段验收矩阵.md`、`docs/数据库系统集成设计.md`、`docs/第三阶段测试报告.md`、`docs/第三阶段答辩逐文件导读.md` 和 `docs/第三阶段综合实训报告.md`。

## 当前边界

核心已实现 CREATE/INSERT/SELECT/DELETE、表达式、精确错误、语义检查、Plan、规则优化、槽页系统目录和页式持久化。SQL 与执行扩展只选 `UPDATE`、`ORDER BY`；存储扩展只选 Extent 分配、页级 redo WAL/Checkpoint。未实现 EXPLAIN、JOIN、GROUP BY、索引、权限、查询并发、MVCC 和代价优化器。

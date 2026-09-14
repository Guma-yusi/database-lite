import tempfile
import time
import unittest
from threading import Event

from os_sim import QueryScheduler, StorageService
from os_sim.errors import CacheAllPinned, InvalidPage, InvalidPageDataSize, QueueOverloaded, WriteBarrierError
from os_sim.page_file import EXTENT_PAGES, PAGE_SIZE, PAYLOAD_SIZE


def payload(text):
    raw = text.encode("utf-8")
    return raw + bytes(PAYLOAD_SIZE - len(raw))


class OSStorageDemoTests(unittest.TestCase):
    """操作系统子系统回归测试：尽量覆盖页管理、缓存、WAL、恢复与并发调度。"""

    def test_01_page_allocation_extent_and_directory(self):
        print("\n===== 1 · 页分配 / extent 扩容 / 页目录 =====")
        with tempfile.TemporaryDirectory() as root:
            store = StorageService(root)

            ids = [store.allocate_page() for _ in range(EXTENT_PAGES + 1)]
            directory = store.page_directory()
            print(f"  连续分配 {EXTENT_PAGES + 1} 页 -> 最后一页 page_id={ids[-1]}，总页数={store.stats()['total_pages']}")
            print(f"  页目录第 0 页偏移={directory[0]['offset']}，第 64 页偏移={directory[64]['offset']}")

            store.free_page(7)
            reused = store.allocate_page()
            print(f"  释放 page 7 后再次分配 -> 复用 page_id={reused}")

            self.assertEqual(ids[-1], EXTENT_PAGES)
            self.assertEqual(store.stats()["total_pages"], EXTENT_PAGES * 2)
            self.assertEqual(directory[0]["offset"], 0)
            self.assertEqual(directory[64]["offset"], EXTENT_PAGES * PAGE_SIZE)
            self.assertEqual(reused, 7)
            store.close()

    def test_02_read_write_validation_and_aliases(self):
        print("\n===== 2 · 页读写 / 错误类型 / 接口别名 =====")
        with tempfile.TemporaryDirectory() as root:
            store = StorageService(root)
            page_id = store.allocate_page()

            lsn = store.write_page(page_id, payload("hello-page"))
            store.flush_page(page_id)
            read_back = store.read_page(page_id)
            alias_read = store.get_page(page_id)
            print(f"  写入 page {page_id} -> LSN {lsn}，读回前 10 字节={read_back[:10]!r}")

            self.assertTrue(read_back.startswith(b"hello-page"))
            self.assertEqual(alias_read, read_back)

            with self.assertRaises(InvalidPageDataSize):
                store.write_page(page_id, b"short")

            store.release_page(page_id)
            with self.assertRaises(InvalidPage):
                store.read_page(page_id)

            store.close()

    def test_03_table_page_mapping_persists_across_restart(self):
        print("\n===== 3 · 表到页集合映射 / 重启恢复 =====")
        with tempfile.TemporaryDirectory() as root:
            store = StorageService(root)
            p0 = store.append_table_page("books")
            p1 = store.append_table_page("books")
            p2 = store.append_table_page("readers")
            store.register_table_page("books", p0)  # 重复注册不应重复写入
            print(f"  books -> {store.get_table_pages('books')}")
            print(f"  readers -> {store.get_table_pages('readers')}")

            self.assertEqual(store.get_table_pages("books"), [p0, p1])
            self.assertEqual(store.get_table_pages("readers"), [p2])
            store.close()

            reopened = StorageService(root)
            self.assertEqual(reopened.get_table_pages("books"), [p0, p1])
            reopened.release_page(p1)
            print(f"  释放 books 的 page {p1} 后 -> {reopened.get_table_pages('books')}")
            self.assertEqual(reopened.get_table_pages("books"), [p0])
            reopened.close()

    def test_04_cache_hits_prefetch_and_recent_events(self):
        print("\n===== 4 · 缓存命中 / 预读 / 事件日志 =====")
        with tempfile.TemporaryDirectory() as root:
            store = StorageService(root, cache_pages=2, policy="LRU")
            p0 = store.allocate_page()
            p1 = store.allocate_page()

            baseline_reads = store.file.reads
            store.read_page(p0)
            store.read_page(p0)
            first_prefetch = store.prefetch_page(p1)
            second_prefetch = store.prefetch_page(p1)
            store.read_page(p1)
            stats = store.stats()
            events = store.cache.recent_events(limit=10)
            names = [event["event"] for event in events]

            print(f"  磁盘读取增加 {store.file.reads - baseline_reads} 次，命中={stats['hits']}，未命中={stats['misses']}")
            print(f"  最近事件={names}")

            self.assertEqual(store.file.reads - baseline_reads, 2)
            self.assertEqual(stats["hits"], 2)
            self.assertEqual(stats["misses"], 1)
            self.assertTrue(first_prefetch)
            self.assertFalse(second_prefetch)
            self.assertIn("prefetch", names)
            self.assertIn("prefetch-skip", names)
            store.close()

    def test_05_replacement_policies_pin_protection_and_replace_log(self):
        print("\n===== 5 · 替换策略 / pin 保护 / replace 日志 =====")

        def resident(policy):
            with tempfile.TemporaryDirectory() as root:
                store = StorageService(root, cache_pages=2, policy=policy)
                pages = [store.allocate_page() for _ in range(3)]
                store.read_page(pages[0])
                store.read_page(pages[1])
                store.read_page(pages[0])
                store.read_page(pages[2])
                keys = set(store.cache.frames)
                events = store.cache.recent_events(limit=20)
                store.close()
                return keys, events

        expected = {
            "LRU": {0, 2},
            "FIFO": {1, 2},
            "LFU": {0, 2},
            "CLOCK": {1, 2},
        }
        for policy, want in expected.items():
            keys, events = resident(policy)
            replace_events = [event for event in events if event["event"] == "replace"]
            print(f"  {policy} 最终驻留 {sorted(keys)}")
            self.assertEqual(keys, want)
            self.assertTrue(replace_events)
            self.assertIn("victim_page_id", replace_events[-1])
            self.assertIn("new_page_id", replace_events[-1])

        with tempfile.TemporaryDirectory() as root:
            store = StorageService(root, cache_pages=2, policy="LRU")
            pages = [store.allocate_page() for _ in range(3)]
            store.cache.fetch(pages[0])  # 保持 pin
            store.cache.fetch(pages[1])  # 保持 pin
            with self.assertRaises(CacheAllPinned):
                store.prefetch_page(pages[2])
            store.cache.unpin(pages[0])
            store.cache.unpin(pages[1])
            store.close()

    def test_06_wal_barrier_flush_and_crash_recovery(self):
        print("\n===== 6 · WAL 屏障 / flush_all / 崩溃恢复 =====")
        with tempfile.TemporaryDirectory() as root:
            store = StorageService(root, cache_pages=2, dirty_ratio=2)
            page_id = store.allocate_page()
            lsn = store.write_page(page_id, payload("committed-in-wal"))
            print(f"  写 page {page_id} -> WAL LSN {lsn}，脏页数={store.stats()['dirty']}")

            store.wal.durable_lsn = 0
            with self.assertRaises(WriteBarrierError):
                store.flush_page(page_id)
            store.wal.durable_lsn = lsn
            checkpoint = store.flush_all()
            print(f"  flush_all 后 checkpoint_lsn={checkpoint['checkpoint_lsn']}")
            self.assertEqual(checkpoint["checkpoint_lsn"], lsn)
            store.close(clean=False)

            crashed = StorageService(root, cache_pages=2, dirty_ratio=2)
            crashed.write_page(page_id, payload("replay-me"))
            crashed.close(clean=False)

            recovered = StorageService(root, cache_pages=2, dirty_ratio=2)
            content = recovered.read_page(page_id)
            print(f"  重启恢复页数={recovered.stats()['recovered_pages']}，读回内容={content[:12].decode()}")
            self.assertGreaterEqual(recovered.stats()["recovered_pages"], 1)
            self.assertTrue(content.startswith(b"replay-me"))
            recovered.close()

    def test_07_background_writer_and_checkpoint_clean_dirty_pages(self):
        print("\n===== 7 · 后台刷盘 / Checkpoint =====")
        with tempfile.TemporaryDirectory() as root:
            store = StorageService(root, cache_pages=4, dirty_ratio=10, background_interval=0.05)
            page_id = store.allocate_page()
            store.write_page(page_id, payload("background-writer"))
            print(f"  写入后脏页数={store.stats()['dirty']}")

            deadline = time.time() + 1.5
            while time.time() < deadline and store.stats()["dirty"]:
                time.sleep(0.05)

            checkpoint = store.checkpoint()
            print(f"  后台刷盘后脏页数={store.stats()['dirty']}，checkpoint_lsn={checkpoint['checkpoint_lsn']}")
            self.assertEqual(store.stats()["dirty"], 0)
            self.assertGreaterEqual(checkpoint["checkpoint_lsn"], 1)
            store.close()

    def test_08_scheduler_backpressure_and_stats(self):
        print("\n===== 8 · 有界并发队列 / 背压 =====")
        gate = Event()
        scheduler = QueryScheduler(workers=1, queue_capacity=1)

        def slow_task(value):
            gate.wait(timeout=1)
            return value * 2

        first = scheduler.submit(slow_task, 3)
        second = scheduler.submit(slow_task, 5)
        with self.assertRaises(QueueOverloaded):
            scheduler.submit(slow_task, 7, block=False)

        gate.set()
        scheduler.drain()
        stats = scheduler.stats()
        print(f"  submitted={stats['submitted']} completed={stats['completed']} rejected={stats['rejected']} max_depth={stats['max_depth']}")

        self.assertEqual(first.result(), 6)
        self.assertEqual(second.result(), 10)
        self.assertEqual(stats["submitted"], 2)
        self.assertEqual(stats["completed"], 2)
        self.assertEqual(stats["rejected"], 1)
        scheduler.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

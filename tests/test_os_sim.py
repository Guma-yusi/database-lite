import tempfile
import unittest

from os_sim import StorageService
from os_sim.errors import WriteBarrierError
from os_sim.page_file import EXTENT_PAGES, PAYLOAD_SIZE


def payload(text):
    raw = text.encode("utf-8")
    return raw + bytes(PAYLOAD_SIZE - len(raw))


class OSStorageDemoTests(unittest.TestCase):
    """操作系统子系统中期检查演示：五个测试按 1~5 顺序依次运行。"""

    def test_01_page_allocation_extent(self):
        print("\n===== 1 · 页分配 / extent 扩容 / 释放复用 =====")
        with tempfile.TemporaryDirectory() as d:
            store = StorageService(d)

            ids = [store.allocate_page() for _ in range(EXTENT_PAGES + 1)]
            print(f"  连续分配 {EXTENT_PAGES + 1} 页：第 65 页返回页号 {ids[-1]}，总页数 {store.stats()['total_pages']}")

            store.release_page(7)
            reused = store.allocate_page()
            print(f"  释放页 7 后再分配：复用页号 {reused}，磁盘偏移 {store.page_directory()[7]['offset']}")

            self.assertEqual(ids[-1], EXTENT_PAGES)
            self.assertEqual(store.stats()["total_pages"], EXTENT_PAGES * 2)
            self.assertEqual(reused, 7)
            self.assertEqual(store.page_directory()[7]["offset"], 7 * 4096)
            store.close()

    def test_02_cache_reduces_disk_reads(self):
        print("\n===== 2 · 页缓存命中统计 =====")
        with tempfile.TemporaryDirectory() as d:
            store = StorageService(d, cache_pages=2)
            pid = store.allocate_page()
            baseline = store.file.reads

            store.read_page(pid)
            for _ in range(99):
                store.read_page(pid)

            stats = store.stats()
            print(f"  连续读同一页 100 次：磁盘读 {store.file.reads - baseline} 次，命中 {stats['hits']}，未命中 {stats['misses']}，命中率 {stats['hit_rate']:.2%}")

            self.assertEqual(store.file.reads - baseline, 1)
            self.assertEqual(stats["hits"], 99)
            store.close()

    def test_03_lru_vs_fifo(self):
        print("\n===== 3 · LRU / FIFO 替换策略 =====")
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            def resident(root, policy):
                s = StorageService(root, cache_pages=2, policy=policy)
                p = [s.allocate_page() for _ in range(3)]
                s.read_page(p[0])
                s.read_page(p[1])
                s.read_page(p[0])
                s.read_page(p[2])
                keys = set(s.cache.frames)
                s.close()
                return keys

            lru = resident(d1, "LRU")
            fifo = resident(d2, "FIFO")

            print(f"  访问顺序 0 → 1 → 0 → 2，缓存容量 2")
            print(f"  LRU 最终驻留 {sorted(lru)}，FIFO 最终驻留 {sorted(fifo)}")

            self.assertEqual(lru, {0, 2})
            self.assertEqual(fifo, {1, 2})

    def test_03b_lfu_and_clock_are_observable(self):
        print("\n===== 3b · LFU / CLOCK 替换策略 =====")
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            def resident(root, policy):
                s = StorageService(root, cache_pages=2, policy=policy)
                p = [s.allocate_page() for _ in range(3)]
                s.read_page(p[0])
                s.read_page(p[1])
                s.read_page(p[0])
                s.read_page(p[2])
                keys = set(s.cache.frames)
                s.close()
                return keys

            lfu = resident(d1, "LFU")
            clock = resident(d2, "CLOCK")

            print("  访问顺序 0 → 1 → 0 → 2，缓存容量 2")
            print(f"  LFU 最终驻留 {sorted(lfu)}，CLOCK 最终驻留 {sorted(clock)}")

            self.assertEqual(lfu, {0, 2})
            self.assertEqual(clock, {1, 2})

    def test_04_wal_barrier(self):
        print("\n===== 4 · WAL 写前日志屏障 =====")
        with tempfile.TemporaryDirectory() as d:
            store = StorageService(d, dirty_ratio=2)
            pid = store.allocate_page()
            lsn = store.write_page(pid, payload("dirty"))
            print(f"  写页 {pid} → WAL LSN {lsn}，脏页 {store.stats()['dirty']}")

            store.wal.durable_lsn = 0
            raised = False
            try:
                store.cache.flush_page(pid)
            except WriteBarrierError:
                raised = True
                print(f"  durable_lsn 拨回 0 后刷盘 → 抛出 WriteBarrierError")

            self.assertTrue(raised)
            store.close(clean=False)

    def test_05_crash_recovery(self):
        print("\n===== 5 · WAL 重放与崩溃恢复 =====")
        with tempfile.TemporaryDirectory() as d:
            crashed = StorageService(d, dirty_ratio=2)
            pid = crashed.allocate_page()
            crashed.write_page(pid, payload("committed-in-wal"))

            recovered = StorageService(d, dirty_ratio=2)
            recovered_count = recovered.stats()["recovered_pages"]
            content = recovered.read_page(pid)

            print(f"  未 checkpoint 直接重启：重放页数 {recovered_count}")
            print(f"  读回页 {pid} 内容 = {content[:16].decode()}")

            self.assertGreaterEqual(recovered_count, 1)
            self.assertTrue(content.startswith(b"committed-in-wal"))
            recovered.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

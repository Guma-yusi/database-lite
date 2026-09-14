import struct
import tempfile
import unittest
from pathlib import Path

from minidb.catalog import ColumnSchema, TableSchema
from minidb.errors import StorageError
from minidb.storage.page import HEADER, PAGE_SIZE, SlottedPage
from minidb.storage.record import RecordCodec
from os_sim import StorageService
from os_sim.cache import PageCache
from os_sim.errors import CorruptPage
from os_sim.page_file import PageFile


class RecordAndSlottedPageTests(unittest.TestCase):
    def setUp(self):
        self.schema = TableSchema("t", [ColumnSchema("id", "INT"), ColumnSchema("text", "VARCHAR", 8)])

    def test_record_round_trip_supports_int64_unicode_and_null(self):
        for row in ({"id": 2**63-1, "text": "中文"}, {"id": -(2**63), "text": None}):
            with self.subTest(row=row):
                self.assertEqual(RecordCodec.decode(self.schema, RecordCodec.encode(self.schema, row)), row)

    def test_malformed_records_raise_storage_error(self):
        valid = RecordCodec.encode(self.schema, {"id": 1, "text": "ok"})
        for data in (b"", valid[:-1], struct.pack("<H", 1) + valid[2:]):
            with self.subTest(size=len(data)), self.assertRaises(StorageError):
                RecordCodec.decode(self.schema, data)

    def test_page_requires_exact_size_and_valid_layout(self):
        with self.assertRaises(StorageError):
            SlottedPage(0, b"short")
        data = bytearray(SlottedPage(0).data)
        magic, page_id, count, start, end, nxt = HEADER.unpack_from(data)
        HEADER.pack_into(data, 0, magic, page_id, count, start + 1, end, nxt)
        with self.assertRaises(StorageError):
            SlottedPage(0, bytes(data))

    def test_slot_boundaries_and_tombstone(self):
        page = SlottedPage(3)
        slot = page.insert(b"abc")
        self.assertEqual(page.read(slot), b"abc")
        self.assertTrue(page.delete(slot))
        self.assertIsNone(page.read(slot))
        with self.assertRaises(StorageError):
            page.read(slot + 1)


class PageFileAndCacheAcceptanceTests(unittest.TestCase):
    def test_configuration_rejects_impossible_cache_values(self):
        class Stub: pass
        with self.assertRaises(ValueError):
            PageCache(Stub(), Stub(), capacity=0)
        with self.assertRaises(ValueError):
            PageCache(Stub(), Stub(), event_limit=0)
        with self.assertRaises(ValueError):
            StorageService("unused", dirty_ratio=0)

    def test_checksum_detects_external_page_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            page_file = PageFile(directory)
            page_id = page_file.allocate_page()
            page_file.write_page(page_id, b"x" * PAGE_SIZE, 2)
            path = Path(directory) / "tablespace.dat"
            with path.open("r+b") as stream:
                stream.seek(page_id * PAGE_SIZE)
                stream.write(b"y")
            with self.assertRaises(CorruptPage):
                page_file.read_page(page_id)

    def test_dirty_threshold_flushes_and_checkpoint_is_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StorageService(directory, cache_pages=2, dirty_ratio=0.5)
            page_id = store.allocate_page()
            lsn = store.write_page(page_id, b"a" * PAGE_SIZE)
            self.assertEqual(store.stats()["dirty"], 0)
            self.assertGreaterEqual(store.stats()["flushes"], 1)
            self.assertEqual(store.checkpoint()["checkpoint_lsn"], lsn)
            store.close()

    def test_one_corrupt_control_copy_falls_back_to_the_other(self):
        with tempfile.TemporaryDirectory() as directory:
            page_file = PageFile(directory)
            page_id = page_file.allocate_page()
            page_file.write_page(page_id, b"z" * PAGE_SIZE, 2)
            (Path(directory) / "control.0.json").write_text("{broken", encoding="utf-8")
            reopened = PageFile(directory)
            self.assertEqual(reopened.read_page(page_id)[0], b"z" * PAGE_SIZE)

    def test_table_page_mapping_persists_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StorageService(directory)
            p0 = store.append_table_page("books")
            p1 = store.append_table_page("books")
            store.close()
            reopened = StorageService(directory)
            self.assertEqual(reopened.get_table_pages("books"), [p0, p1])
            reopened.close()

    def test_replace_log_contains_victim_new_page_and_flush_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StorageService(directory, cache_pages=2, policy="LRU")
            pages = [store.allocate_page() for _ in range(3)]
            for page_id in pages[:2]:
                store.read_page(page_id)
            store.write_page(pages[0], b"d" * PAGE_SIZE)
            store.read_page(pages[2])
            replace = [e for e in store.cache.recent_events(20) if e["event"] == "replace"][-1]
            self.assertIn("victim_page_id", replace)
            self.assertIn("new_page_id", replace)
            self.assertIn("dirty", replace)
            self.assertIn("flushed", replace)
            store.close()


if __name__ == "__main__":
    unittest.main()

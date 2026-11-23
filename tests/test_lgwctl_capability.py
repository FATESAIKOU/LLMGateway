"""``lgwctl.capability`` 的向量表（#246 K-1／T-8）：四態與 old／new 的對應。

只驗 llmgwctl 這一份解析器；隧道腳本內嵌的那一份是 PR-B 的事（T-9 留給
PR-B：同一張表餵兩邊逐格比對）。

向量用暫存目錄造檔：正常檔、缺檔、空檔、非 UTF-8、非 JSON、array、``schema``
不是整數 1、``probe`` 不是 ``healthz``、多鍵、少鍵、重複鍵、超過 4096 bytes、
最後一段是 symlink、是目錄、EACCES。
"""

import json
import os
import tempfile
import unittest

from lgwctl import capability

VALID = '{"schema": 1, "probe": "healthz"}\n'


class VectorTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.version = os.path.join(self.root, "lgw-v1.2.3")
        os.makedirs(os.path.join(self.version, "deploy"), exist_ok=True)

    # -- 工具 ---------------------------------------------------------------

    def path(self) -> str:
        return os.path.join(self.version, capability.CAPABILITIES_REL)

    def put(self, text) -> None:
        raw = text if isinstance(text, bytes) else text.encode("utf-8")
        with open(self.path(), "wb") as fh:
            fh.write(raw)

    def read(self):
        return capability.read(self.version)

    # -- 正向 ---------------------------------------------------------------

    def test_positive_is_ok_and_new(self):
        """正向：{"schema": 1, "probe": "healthz"} → ok／new。"""
        self.put(VALID)
        cap = self.read()
        self.assertEqual((cap.state, cap.mode), ("ok", "new"))

    def test_state_and_mode_are_frozen(self):
        """Capability 是不可變的（呼叫方不該改它）。"""
        self.put(VALID)
        cap = self.read()
        with self.assertRaises(Exception):
            cap.state = "missing"

    # -- 負向：每一格都要 invalid／old -------------------------------------

    def test_missing_file_is_missing_and_old(self):
        """缺檔（回滾到沒有這個檔的舊版）→ missing／old，不是錯。"""
        cap = self.read()
        self.assertEqual((cap.state, cap.mode), ("missing", "old"))

    def test_empty_file_is_invalid(self):
        self.put("")
        self.assertEqual(self.read(), capability.INVALID)

    def test_non_utf8_is_invalid(self):
        self.put(b'{"schema": 1, "probe": "healthz\xff"}\n')
        self.assertEqual(self.read(), capability.INVALID)

    def test_not_json_is_invalid(self):
        self.put("not json\n")
        self.assertEqual(self.read(), capability.INVALID)

    def test_array_is_invalid(self):
        self.put('[1]\n')
        self.assertEqual(self.read(), capability.INVALID)

    def test_schema_2_is_invalid(self):
        self.put('{"schema": 2, "probe": "healthz"}\n')
        self.assertEqual(self.read(), capability.INVALID)

    def test_schema_true_is_invalid(self):
        """True 不算整數（bool 是 int 的子類，要明確排除）。"""
        self.put('{"schema": true, "probe": "healthz"}\n')
        self.assertEqual(self.read(), capability.INVALID)

    def test_schema_string_is_invalid(self):
        self.put('{"schema": "1", "probe": "healthz"}\n')
        self.assertEqual(self.read(), capability.INVALID)

    def test_probe_list_is_invalid(self):
        self.put('{"schema": 1, "probe": "list"}\n')
        self.assertEqual(self.read(), capability.INVALID)

    def test_extra_key_is_invalid(self):
        self.put('{"schema": 1, "probe": "healthz", "x": 1}\n')
        self.assertEqual(self.read(), capability.INVALID)

    def test_missing_key_is_invalid(self):
        for text in ('{"schema": 1}\n', '{"probe": "healthz"}\n'):
            with self.subTest(text=text):
                self.put(text)
                self.assertEqual(self.read(), capability.INVALID)

    def test_duplicate_key_is_invalid(self):
        """重複鍵：json 預設留最後一個，這裡一律算 invalid。"""
        self.put('{"schema": 1, "probe": "healthz", "probe": "healthz"}\n')
        self.assertEqual(self.read(), capability.INVALID)

    def test_over_4096_bytes_is_invalid(self):
        raw = (" " * (capability.MAX_BYTES + 1)).encode() + VALID.encode()
        self.put(raw)
        self.assertEqual(self.read(), capability.INVALID)

    def test_exactly_4096_bytes_is_fine(self):
        """上限是「≤4096」：剛好 4096 的合法內容仍然 ok（補在後面的空白）。"""
        body = VALID.rstrip("\n")
        padded = body + " " * (capability.MAX_BYTES - len(body))
        self.assertEqual(len(padded), capability.MAX_BYTES)
        self.put(padded)
        self.assertEqual((self.read().state, self.read().mode), ("ok", "new"))

    def test_symlink_as_last_segment_is_invalid(self):
        """最後一段是 symlink → invalid（開檔用 O_NOFOLLOW）。"""
        target = os.path.join(self.root, "elsewhere.json")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(VALID)
        os.symlink(target, self.path())
        self.assertEqual(self.read(), capability.INVALID)

    def test_directory_is_invalid(self):
        os.makedirs(self.path())
        self.assertEqual(self.read(), capability.INVALID)

    def test_eacces_is_unreadable(self):
        """EACCES → unreadable／old（root 不受權限限制，跳過）。"""
        if os.geteuid() == 0:
            self.skipTest("root 不受檔案權限限制，無法造 EACCES")
        self.put(VALID)
        os.chmod(self.path(), 0o000)
        self.addCleanup(os.chmod, self.path(), 0o644)
        self.assertEqual(self.read(), capability.UNREADABLE)

    # -- current()：跟著 current symlink 走 ---------------------------------

    def test_current_follows_the_link(self):
        """current(layout) = read(current symlink)：跟著路徑上的 symlink 走。"""
        self.put(VALID)
        link = os.path.join(self.root, "current")
        os.symlink(self.version, link)

        class FakeLayout:
            current_link = link

        self.assertEqual(capability.current(FakeLayout()), capability.OK)

    def test_current_missing_link_is_missing(self):
        class FakeLayout:
            current_link = os.path.join(self.root, "current")

        cap = capability.current(FakeLayout())
        self.assertEqual((cap.state, cap.mode), ("missing", "old"))

    # -- precheck：切換前的驗證（K-3）---------------------------------------

    def test_precheck_reports_each_state(self):
        from lgwctl.report import Report

        cases = (
            (capability.OK, "ok", "new"),
            (capability.MISSING, "ok", "無（old）"),
            (capability.INVALID, "fail", None),
            (capability.UNREADABLE, "fail", None),
        )
        for cap, level, detail in cases:
            with self.subTest(state=cap.state):
                report = Report()
                allowed = capability.precheck(report, cap)
                self.assertEqual(allowed, level == "ok")
                line = report.lines[0]
                self.assertTrue(line.startswith(level + " capabilities"), line)
                if detail is not None:
                    self.assertIn(detail, line)


if __name__ == "__main__":
    unittest.main()

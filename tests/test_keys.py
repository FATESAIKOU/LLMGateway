"""KeyStore 的測試：讀檔、重讀、空字串、格式錯誤保留舊值、不含值保證。只用假資料。"""

import tempfile
import unittest
from pathlib import Path

from lgwd.keys import KEY_NAMES, KeyFileError, KeyStore

# 假資料：絕不是真的 key
CANARY = "canary-4d2c8a-value-never-real"
OTHER = "canary-other-fake-value"


def _write(path, text):
    Path(path).write_text(text, encoding="utf-8")


class KeyStoreBasicTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = str(Path(self.tmpdir.name) / "keys.env")

    def test_get_and_deployed(self):
        _write(self.path, f"OPENROUTER_API_KEY={CANARY}\n# 註解\n\nOLLAMA_API_KEY={OTHER}\n")
        store = KeyStore(self.path)
        self.assertEqual(store.get("openrouter"), CANARY)
        self.assertTrue(store.deployed("openrouter"))
        self.assertTrue(store.deployed("ollama-cloud"))
        self.assertIsNone(store.last_reload_error)

    def test_empty_string_counts_as_not_deployed(self):
        _write(self.path, "OPENROUTER_API_KEY=\n")
        store = KeyStore(self.path)
        self.assertIsNone(store.get("openrouter"))
        self.assertFalse(store.deployed("openrouter"))

    def test_missing_key_counts_as_not_deployed(self):
        _write(self.path, "# 空檔，只剩註解\n")
        store = KeyStore(self.path)
        self.assertIsNone(store.get("openrouter"))
        self.assertFalse(store.deployed("openrouter"))

    def test_unknown_subscription_raises(self):
        _write(self.path, "")
        store = KeyStore(self.path)
        with self.assertRaises(KeyFileError):
            store.get("ghost")
        with self.assertRaises(KeyFileError):
            store.deployed("ghost")

    def test_missing_file_raises(self):
        with self.assertRaises(KeyFileError):
            KeyStore(str(Path(self.tmpdir.name) / "no-such-file.env"))

    def test_key_names_cover_all_subscriptions(self):
        self.assertEqual(
            set(KEY_NAMES),
            {"claude-personal", "claude-company", "openrouter", "opencode-zen",
             "opencode-go", "ollama-cloud"},
        )

    def test_opencode_go_共用zen的鍵名(self):
        # #77：Go 與 Zen 共用 OPENCODE_ZEN_API_KEY；key 檔只寫一行，兩個訂閱都讀到
        self.assertEqual(KEY_NAMES["opencode-go"], "OPENCODE_ZEN_API_KEY")
        _write(self.path, f"OPENCODE_ZEN_API_KEY={CANARY}\n")
        store = KeyStore(self.path)
        self.assertEqual(store.get("opencode-zen"), CANARY)
        self.assertEqual(store.get("opencode-go"), CANARY)
        self.assertTrue(store.deployed("opencode-zen"))
        self.assertTrue(store.deployed("opencode-go"))


class KeyStoreReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = str(Path(self.tmpdir.name) / "keys.env")
        _write(self.path, f"OPENROUTER_API_KEY={CANARY}\n")
        self.store = KeyStore(self.path)

    def test_reload_replaces_values(self):
        _write(self.path, f"OPENROUTER_API_KEY={OTHER}\nOLLAMA_API_KEY={CANARY}\n")
        self.store.reload()
        self.assertEqual(self.store.get("openrouter"), OTHER)
        self.assertEqual(self.store.get("ollama-cloud"), CANARY)
        self.assertIsNone(self.store.last_reload_error)

    def test_bad_format_keeps_old_values(self):
        _write(self.path, "這行沒有等號\n")
        with self.assertRaises(KeyFileError):
            self.store.reload()
        self.assertEqual(self.store.get("openrouter"), CANARY)
        self.assertIsNotNone(self.store.last_reload_error)
        self.assertNotIn(CANARY, self.store.last_reload_error)

    def test_unknown_key_keeps_old_values(self):
        _write(self.path, f"NOPE_NOT_A_KEY={CANARY}\n")
        with self.assertRaises(KeyFileError) as ctx:
            self.store.reload()
        self.assertEqual(self.store.get("openrouter"), CANARY)
        self.assertNotIn(CANARY, str(ctx.exception))

    def test_duplicate_key_rejected(self):
        _write(self.path, f"OPENROUTER_API_KEY={CANARY}\nOPENROUTER_API_KEY={OTHER}\n")
        with self.assertRaises(KeyFileError):
            self.store.reload()
        self.assertEqual(self.store.get("openrouter"), CANARY)

    def test_unreadable_file_keeps_old_values(self):
        Path(self.path).unlink()  # 檔被拿掉，模擬部署中斷
        with self.assertRaises(KeyFileError):
            self.store.reload()
        self.assertEqual(self.store.get("openrouter"), CANARY)

    def test_whitespace_and_comments_ignored(self):
        _write(self.path, f"  OPENROUTER_API_KEY  =  {CANARY}  \n# c\n\n")
        self.store.reload()
        self.assertEqual(self.store.get("openrouter"), CANARY)

    def test_no_values_in_repr_or_errors(self):
        self.assertEqual(repr(self.store), "<KeyStore>")
        self.assertNotIn(CANARY, repr(self.store))
        _write(self.path, f"BOGUS={CANARY}\n")
        try:
            self.store.reload()
        except KeyFileError as exc:
            self.assertNotIn(CANARY, str(exc))
        else:
            self.fail("expected KeyFileError")
        self.assertNotIn(CANARY, self.store.last_reload_error or "")

"""包裝與相依（PM 2026-10-12）：lgw 只依賴標準函式庫；pyproject.toml 的形狀。

sidecar 會用 `pip install "lgw @ git+https://github.com/FATESAIKOU/LLMGateway@lgw-v0.1.0"`
依賴 lgw，所以 repo 根目錄的 pyproject.toml 只打包 lgw（不含 lgwd、tests、deploy）。
"""

from __future__ import annotations

import ast
import sys
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LGW = REPO / "lgw"
ALLOWED_ROOTS = set(sys.stdlib_module_names) | {"lgw"}


class ImportsTest(unittest.TestCase):
    def test_lgw_imports_stdlib_only(self):
        """掃 lgw/*.py：只准標準函式庫與 lgw 自己（不准 lgwd、第三方套件）。"""
        seen = 0
        for path in sorted(LGW.glob("*.py")):
            seen += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            roots: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:  # 相對匯入＝lgw 自己
                        continue
                    if node.module:
                        roots.add(node.module.split(".")[0])
            offenders = roots - ALLOWED_ROOTS
            self.assertFalse(offenders, "{0}: {1}".format(path.name, sorted(offenders)))
        self.assertGreaterEqual(seen, 4)  # __init__、client、connect、signals

    def test_lazy_submodules(self):
        """`import lgw` 後就有 lgw.connect.connect 與 lgw.client.Client（docstring 的用法）。"""
        import lgw

        self.assertTrue(callable(lgw.connect.connect))
        self.assertTrue(hasattr(lgw.client, "Client"))
        self.assertTrue(hasattr(lgw.signals, "classify"))


class PyprojectTest(unittest.TestCase):
    def setUp(self):
        self.data = tomllib.loads(
            (REPO / "pyproject.toml").read_text(encoding="utf-8")
        )

    def test_project_fields(self):
        project = self.data["project"]
        self.assertEqual(project["name"], "lgw")
        self.assertEqual(project["version"], "0.1.0")
        self.assertEqual(project["requires-python"], ">=3.10")
        self.assertEqual(project.get("dependencies", []), [])

    def test_build_backend_and_packages(self):
        self.assertEqual(
            self.data["build-system"]["build-backend"], "setuptools.build_meta"
        )
        self.assertEqual(self.data["tool"]["setuptools"]["packages"], ["lgw"])


if __name__ == "__main__":
    unittest.main()

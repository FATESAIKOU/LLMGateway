"""測試起真 lgwd 子行程時用的政策 helper（FIX-3）。

`usage_all_disabled()` 回傳的 `usage` 段，把所有「有 usage fetcher」的 provider
都設成關閉，避免測試對外連線。直接從 `lgwd.providers.PROVIDERS` 推導：
凡是 `make_usage_fetcher` 不回 None 的 provider（且在出廠政策的 subscriptions 裡）
都關掉，這樣以後新增 fetcher 會自動被關。
"""

from __future__ import annotations

import json
from pathlib import Path

from lgwd import providers as _providers

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _fetcher_providers() -> list[str]:
    policy = json.loads((_REPO_ROOT / "policy" / "default.json").read_text(encoding="utf-8"))
    subs = set(policy["subscriptions"])
    deps = _providers.UsageDeps()
    return [name for name, prov in _providers.PROVIDERS.items()
            if name in subs and prov.make_usage_fetcher(deps) is not None]


def usage_all_disabled() -> dict:
    return {name: {"enabled": False} for name in _fetcher_providers()}
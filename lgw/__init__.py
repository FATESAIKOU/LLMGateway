"""LLMGateway 客戶端（lgw）：連線程式、三個介面的客戶端、額度訊號規則。

只用 Python 標準函式庫。

- `lgw.connect`：with lgw.connect.connect(cfg) as base_url（每次操作才連、用完就關，
  不長期持有、不自動重連；連不上丟 Unavailable，只帶錯誤種類）。
- `lgw.client`：list／get／登錄冷卻（不帶認證標頭；連不上回「無法確認」）。
- `lgw.signals`：額度訊號規則，給 sidecar 的 AgentDriver 當函式庫用。

規格：openspec/changes/gateway-v0/（design 決定 4、6、9）。
"""

from __future__ import annotations

import importlib

_SUBMODULES = ("client", "connect", "signals")

__all__ = list(_SUBMODULES)


def __getattr__(name: str):
    """需要時才載入子模組（`lgw.connect` 等）；`python -m lgw.connect` 不先載入它。"""
    if name in _SUBMODULES:
        return importlib.import_module("." + name, __name__)
    raise AttributeError("module 'lgw' has no attribute {0!r}".format(name))

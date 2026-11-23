"""LLMGateway 客戶端（lgw）：三個介面的客戶端、額度訊號規則、SSHSIG 簽章。

只用 Python 標準函式庫。

- `lgw.client`：list／resolve（provider＋model，可釘住／排除）／登錄冷卻
  （`signer=None` 時不帶認證標頭；連不上回「無法確認」）。
- `lgw.signer`：用本機 `ssh-keygen -Y sign -n llmgateway` 簽五個 `X-LGW-*` 標頭
  （#222 D4a；私鑰檔或公鑰檔＋ssh-agent；不新增 Python 相依）。
- `lgw.signals`：額度訊號規則，給 sidecar 的 AgentDriver 當函式庫用。

規格：openspec/changes/gateway-v0/（design 決定 6、9；v5 拿掉客戶端連線程式）、openspec/changes/agent-provider-model-interface/（resolve）、openspec/changes/lgw-resolve-auth/（D4 簽章）。
"""

from __future__ import annotations

import importlib

_SUBMODULES = ("client", "signals", "signer")

__all__ = list(_SUBMODULES)


def __getattr__(name: str):
    """需要時才載入子模組（`lgw.client` 等）；`python -m lgw.client` 不先載入它。"""
    if name in _SUBMODULES:
        return importlib.import_module("." + name, __name__)
    raise AttributeError("module 'lgw' has no attribute {0!r}".format(name))

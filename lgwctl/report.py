"""ok／todo／fail 三態的輸出行（#35 spec：每項一行，結束碼 0／1／2）。

- ``ok``：確定了、沒問題；
- ``todo``：要人做某件事，或無法確認（不是壞掉）；
- ``fail``：確定不成立（壞掉）。

``check`` 的結束碼：0 全好、1 有 fail、2 只有 todo。``install`` 把 todo 當正常
（本人的動作還沒做不算失敗），用 ``exit_code(todo_ok=True)``。

輸出絕不含 key 的值或解密密碼：detail 只寫有無、名稱、時間與種類。
"""

from __future__ import annotations

LEVELS = ("ok", "todo", "fail")

#: #289：check 在 unit 裡跑時的 syslog 優先級前綴（journal 的等級）。設了
#: ``LLMGW_CHECK_JOURNAL=1`` 才加；沒設時輸出逐 byte 不變（手動 check 不受影響）。
JOURNAL_PREFIXES = {"fail": "<3>", "todo": "<4>", "ok": "<6>"}


class Report:
    """收集並即時印出三態行；``out`` 預設 print（測試可傳自己的收集器）。

    ``journal=True`` 時印出的每一行前加 ``<3>``／``<4>``／``<6>``（C-982）；
    ``self.lines`` 存的仍是沒有前綴的行（三態判斷與測試都用它）。
    """

    def __init__(self, out=None, journal: bool = False):
        self.lines: list[str] = []
        self._out = print if out is None else out
        self.journal = journal

    def add(self, level: str, name: str, detail: str = "") -> None:
        if level not in LEVELS:
            raise ValueError(f"unknown level: {level}")
        line = f"{level} {name}" + (f"（{detail}）" if detail else "")
        self.lines.append(line)
        self._out(JOURNAL_PREFIXES[level] + line if self.journal else line)

    def ok(self, name: str, detail: str = "") -> None:
        self.add("ok", name, detail)

    def todo(self, name: str, detail: str = "") -> None:
        self.add("todo", name, detail)

    def fail(self, name: str, detail: str = "") -> None:
        self.add("fail", name, detail)

    @property
    def has_fail(self) -> bool:
        return any(line.startswith("fail ") for line in self.lines)

    @property
    def has_todo(self) -> bool:
        return any(line.startswith("todo ") for line in self.lines)

    def level_of(self, name: str) -> str | None:
        """某個項目最後一次的三態（沒有就 None）；給測試與「同項目多行」用。"""
        found = None
        for line in self.lines:
            level, _, rest = line.partition(" ")
            if rest.split("（", 1)[0] == name:
                found = level
        return found

    def exit_code(self, *, todo_ok: bool = False) -> int:
        if self.has_fail:
            return 1
        if self.has_todo and not todo_ok:
            return 2
        return 0

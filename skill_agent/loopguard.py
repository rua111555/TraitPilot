"""死循环 / 无进展检测（M3 LoopGuard）。

借鉴 OpenHands ``controller/stuck.py`` 的 StuckDetector 思路（滑动窗口模式匹配），
但做了 **progress-aware** 改造以适应本机的生信编排管线（WGS/GWAS 会大量合法地
「Bash background → TaskOutput 轮询 → 检查 .done/.pid」）：

- **动作指纹** = (工具名, 规范化后的 input)
- **观测指纹** = 归一化后的 tool_result（剥离行号/时间戳/进度/pid 等易变噪声）
- **进展信号** = 来自 ``ToolResult.meta``（M3a）：文件 mtime/size 变化、日志行数增长、
  退出码改善、后台任务新起、Skill 首次激活 —— 任一成立即视为「有进展」，计数回落

判定：连续「同动作 + 无进展」达到 warn 阈值→仅在 tool_result 里注入纠偏提示；达到
halt 阈值→熔断该轮。**轮询类工具**（TaskOutput / 读 ``*.log``/``.done``/``.pid``）只降级
记录、**永不硬停**（它们的重复等待是合法的），超时兜底交给 M1b 预算。

默认关闭（``enabled=False``）→ 行为与改造前完全一致；可用 ``--loop-guard`` 开启。
"""
from __future__ import annotations

import hashlib
import json
import re
import enum
from dataclasses import dataclass, field

_LINE_NO = re.compile(r"^\s*\d+\t", re.M)
_DIGITS = re.compile(r"\d+")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")
_POLL_SUFFIX = (".log", ".done", ".pid")


class GuardLevel(str, enum.Enum):
    OK = "ok"
    WARN = "warn"
    HALT = "halt"


@dataclass
class Verdict:
    level: GuardLevel = GuardLevel.OK
    count: int = 0
    message: str = ""


def normalize_observation(text: str) -> str:
    """把观测归一化：剥行号/时间戳/长 hex/数字，便于判定「同样的结果」。"""
    if not text:
        return ""
    t = _TS.sub("<T>", text)
    t = _LINE_NO.sub("", t)
    t = _HEX.sub("<H>", t)
    t = _DIGITS.sub("#", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:2000]


def action_fingerprint(name: str, args: dict) -> str:
    try:
        a = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        a = str(args)
    return hashlib.sha1(f"{name}|{a}".encode("utf-8")).hexdigest()[:16]


def observation_fingerprint(text: str) -> str:
    return hashlib.sha1(normalize_observation(text).encode("utf-8")).hexdigest()[:16]


def is_polling(name: str, args: dict) -> bool:
    """轮询类工具：TaskOutput，或读取 *.log/*.done/*.pid 的 Read/Bash。"""
    if name == "TaskOutput":
        return True
    blob = " ".join(str(v) for v in (args or {}).values())
    if any(blob.endswith(s) or s in blob for s in _POLL_SUFFIX):
        return True
    return False


def _made_progress(meta: dict | None, prev_meta: dict | None) -> bool:
    if not meta:
        return False
    if meta.get("task_id"):                       # 新起后台任务
        return True
    if meta.get("lines_total") is not None and prev_meta \
            and prev_meta.get("lines_total") is not None:
        if meta["lines_total"] > prev_meta["lines_total"]:   # 日志增长
            return True
    if meta.get("mtime") is not None and prev_meta \
            and prev_meta.get("mtime") is not None:
        if meta["mtime"] > prev_meta["mtime"]:    # 文件被写
            return True
    if meta.get("exit_code") == 0 and (prev_meta or {}).get("exit_code") not in (0, None):
        return True                               # 从失败转为成功
    if meta.get("pid_alive") is True and (prev_meta or {}).get("pid_alive") is False:
        return True
    return False


@dataclass
class LoopGuard:
    enabled: bool = False
    warn: int = 3
    halt: int = 6
    window: int = 12
    _last_action: str = ""
    _last_obs: str = ""
    _last_meta: dict | None = None
    _repeat: int = 0
    _history: list = field(default_factory=list)

    def reset(self) -> None:
        self._last_action = self._last_obs = ""
        self._last_meta = None
        self._repeat = 0
        self._history = []

    def record(self, name: str, args: dict, content: str,
               meta: dict | None = None) -> Verdict:
        if not self.enabled:
            return Verdict()
        afp = action_fingerprint(name, args)
        ofp = observation_fingerprint(content)
        progress = _made_progress(meta, self._last_meta)

        same_action = (afp == self._last_action)
        if same_action and not progress:
            self._repeat += 1
        else:
            self._repeat = 0 if progress else 1
        self._last_action, self._last_obs, self._last_meta = afp, ofp, meta
        self._history.append((afp, ofp, progress))
        self._history = self._history[-self.window:]

        polling = is_polling(name, args)
        if self._repeat >= self.halt and not polling:
            return Verdict(GuardLevel.HALT, self._repeat,
                           f"检测到无进展循环：`{name}` 已重复 {self._repeat} 次且无状态变化，"
                           f"已熔断该轮以避免空转。请改变策略或检查命令是否正确。")
        if self._repeat >= self.warn:
            if polling:
                return Verdict(GuardLevel.WARN, self._repeat,
                               f"轮询无新进展（{self._repeat} 次）——若长时间无变化请检查后台任务状态。")
            return Verdict(GuardLevel.WARN, self._repeat,
                           f"疑似重复：`{name}` 已连续 {self._repeat} 次且无进展，"
                           f"请确认是否陷入循环并调整策略。")
        return Verdict()

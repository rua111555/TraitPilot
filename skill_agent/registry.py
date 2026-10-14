"""可插拔工具注册表（M4）。

把 ``ToolSet.__init__`` 里写死的「8 个工具字面量列表」解耦为运行时 ``ToolRegistry``：

- 工具以**数据**登记（name / description / input_schema / executor / always / source），
  而不是绑定为 ``ToolSet`` 的方法。
- 支持 ``register`` / ``unregister`` / ``replace`` / ``get`` / ``names`` / ``schemas``。
- 支持**声明式凭据目录**：从 ``<dir>/*.json`` 读取工具规格并装载其 Python 入口。
  这是**显式 opt-in** 的能力（默认不扫描、不存在即不生效），因为动态工具会扩大执行面，
  必须与 M8a 权限门联动。加载失败一律静默跳过，绝不影响内置工具。

安全模型：动态工具与内置工具**同权限执行**，因此默认关闭，且加载前可经 ``allow`` 白名单过滤。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from .tools import Tool   # Tool 是纯数据容器（本模块不参与工具实现）


class ToolRegistry:
    """工具登记与查找。``source`` 记录来源（builtin / plugin:<path>）。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    # ---------------- 基本操作 ----------------
    def register(self, tool: Tool, source: str = "builtin") -> None:
        if not getattr(tool, "name", ""):
            raise ValueError("工具必须有 name")
        setattr(tool, "source", source)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def replace(self, tool: Tool, source: str = "builtin") -> None:
        self.register(tool, source)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, allowed: set[str] | None = None,
                always: tuple[str, ...] = ("Skill", "TaskOutput")) -> list[dict]:
        """返回 API 工具定义。``allowed`` 为 None → 全部；否则按白名单过滤 + 保底。"""
        if allowed:
            names = {n for n in allowed if n in self._tools}
            names |= set(always)
            return [t.api_schema for n, t in self._tools.items() if n in names]
        return [t.api_schema for t in self._tools.values()]

    def by_source(self, prefix: str) -> list[str]:
        return [n for n, t in self._tools.items()
                if str(getattr(t, "source", "")).startswith(prefix)]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


# ---------------- 声明式凭据目录（opt-in） ----------------

def load_plugin_dir(registry: ToolRegistry, directory: Path,
                    allow: list[str] | None = None) -> list[str]:
    """从目录加载 ``*.json`` 工具规格并登记其 executor。

    规格格式::

        {
          "name": "MyTool",
          "description": "...",
          "input_schema": {...},
          "module": "mypkg.mymodule",     # 或 "path": "/abs/entry.py"
          "executor": "make_executor"     # 该模块内的工厂名，返回 callable(args)->ToolResult
        }

    仅加载 ``allow`` 中的名字（若给出）；任何异常一律跳过，返回实际加载的名字列表。
    """
    loaded: list[str] = []
    directory = Path(directory)
    if not directory.is_dir():
        return loaded
    for spec_path in sorted(directory.glob("*.json")):
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = spec.get("name")
        if not name or (allow is not None and name not in allow):
            continue
        try:
            factory = _resolve_factory(spec)
            executor = factory()
            tool = Tool(name=name,
                        description=spec.get("description", f"插件工具 {name}"),
                        input_schema=spec.get("input_schema") or {"type": "object"},
                        executor=executor,
                        always=bool(spec.get("always", False)))
            registry.register(tool, source=f"plugin:{spec_path}")
            loaded.append(name)
        except Exception:
            continue
    return loaded


def _resolve_factory(spec: dict):
    module_ref = spec.get("module")
    path_ref = spec.get("path")
    factory_name = spec.get("executor") or "make_executor"
    if path_ref:
        p = Path(path_ref)
        if not p.is_file():
            raise FileNotFoundError(path_ref)
        mod_spec = importlib.util.spec_from_file_location(f"_plug_{p.stem}", p)
        module = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(module)      # type: ignore[union-attr]
    elif module_ref:
        module = importlib.import_module(module_ref)
    else:
        raise ValueError("规格缺少 module/path")
    factory = getattr(module, factory_name)
    if not callable(factory):
        raise TypeError(f"{factory_name} 不是可调用工厂")
    return factory

"""工具输入 JSON Schema 校验（M6）。

在 ``ToolSet.execute`` 调用 executor 之前，按工具自身的 ``input_schema`` 做
required / type / enum 校验，不合法时返回结构化报错，让模型自我纠正，而不是让
畸形参数落进 executor 后抛出泛化的 ``[异常]``（例如 ``int(args.get('timeout'))``
遇到字符串）。

设计要点（来自 harness 增补方案的 M6）：
- 纯前置校验，不改执行器、不改主循环；校验器本身无副作用、可独立单测。
- 不引入新依赖：轻量手写，只覆盖 harness 实际会用到的一小撮 JSON Schema 关键字
  （type / properties / required / enum / items / additionalProperties）。
- **宽容优先**：只对「required 缺失」与「明显类型错误」报错；可选字段缺失一律放行；
  无法识别的 schema 关键字一律忽略。避免过严拒绝模型合法的宽松输入
  （如把数字传成字符串）——这与现有 executor 的 ``args.get`` 容错保持一致。

典型用法::

    from .validation import validate_args
    err = validate_args(tool.input_schema, args)   # 返回 None 表示通过
    if err:
        return ToolResult(err, is_error=True)
"""
from __future__ import annotations

from typing import Any

# JSON Schema type 名 → 可接受的 python 类型集合
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
    "null": (type(None),),
}


def _typename(value: Any) -> str:
    if value is None:
        return "null"
    return {
        bool: "boolean", int: "integer", float: "number",
        str: "string", list: "array", tuple: "array", dict: "object",
    }.get(type(value), type(value).__name__)


def _numeric_string_ok(value: Any, expected: str) -> bool:
    """数字被写成字符串时照样放行（并允许 executor 自己 ``int(...)``）。

    模型很常把 ``"timeout": "5"`` 写出来；执行器的 ``int(args.get('timeout'))`` 能吃下，
    所以不该在校验层拦。只有**确实转不成数**的字符串（如 ``"abc"``）才算类型错。
    """
    if not isinstance(value, str) or expected not in ("integer", "number"):
        return False
    s = value.strip()
    if not s or s == "-":
        return False
    try:
        num = float(s)
    except ValueError:
        return False
    if expected == "integer":
        return num.is_integer()
    return True


def _type_ok(value: Any, expected: Any) -> bool:
    """宽松类型匹配。

    - expected 为字符串：查表；未知类型名放行（不认识就不拦）。
    - expected 为 list：任一匹配即可（JSON Schema 联合类型）。
    - ``bool`` 是 ``int`` 的子类，需特判：声明 integer/number 时不接受 bool。
    - **数字型字符串放行**（``"5"`` 对 integer、``"1.5"`` 对 number）——docstring
      承诺的「宽容优先…避免把数字传成字符串判错」在此兑现（D8）。
    """
    if isinstance(expected, list):
        return any(_type_ok(value, e) for e in expected)
    if not isinstance(expected, str) or expected not in _TYPE_MAP:
        return True
    if isinstance(value, bool) and expected in ("integer", "number"):
        return False
    if isinstance(value, str) and expected in ("integer", "number"):
        return _numeric_string_ok(value, expected)
    return isinstance(value, _TYPE_MAP[expected])


def _check(node: Any, schema: Any, path: str) -> str | None:
    """递归校验 value ``node`` 是否符合 ``schema``；返回错误消息或 None。"""
    if not isinstance(schema, dict):
        return None

    # 1) enum
    enum = schema.get("enum")
    if isinstance(enum, list) and node not in enum:
        return f"{path}: 取值 {node!r} 不在允许的枚举 {enum} 中"

    # 2) type
    if "type" in schema and not _type_ok(node, schema["type"]):
        exp = schema["type"]
        exp_s = "/".join(exp) if isinstance(exp, list) else str(exp)
        return f"{path}: 期望类型 {exp_s}，实际为 {_typename(node)}"

    # 3) object：properties / required
    if isinstance(node, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for key in schema.get("required") or []:
                if key not in node:
                    return f"{path}: 缺少必填字段 `{key}`"
            for key, sub in props.items():
                if key in node:
                    err = _check(node[key], sub, f"{path}.{key}")
                    if err:
                        return err

    # 4) array：items
    if isinstance(node, (list, tuple)):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(node):
                err = _check(v, items, f"{path}[{i}]")
                if err:
                    return err
    return None


def validate_args(input_schema: Any, args: Any) -> str | None:
    """校验工具调用参数。

    返回 ``None`` 表示通过；否则返回一句人类可读、可回灌给模型的错误说明。
    只做「明显错误」拦截，绝不因为 schema 表达力不足而误杀。
    """
    if not isinstance(input_schema, dict) or not input_schema:
        return None
    if not isinstance(args, dict):
        return f"参数必须是 JSON 对象，实际为 {_typename(args)}"

    err = _check(args, input_schema, "参数")
    if err is None:
        return None
    return (
        f"[输入校验失败] {err}\n"
        f"请按工具 schema 修正参数后重试。期望 schema：\n"
        f"{_schema_hint(input_schema)}"
    )


def _schema_hint(schema: dict) -> str:
    """给模型看的最小 schema 提示（required + 各字段类型）。"""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not isinstance(props, dict) or not props:
        return "(无字段约束)"
    rows = []
    for name, spec in props.items():
        t = spec.get("type") if isinstance(spec, dict) else None
        t_s = "/".join(t) if isinstance(t, list) else (t or "any")
        mark = " (必需)" if name in required else " (可选)"
        desc = spec.get("description", "") if isinstance(spec, dict) else ""
        rows.append(f"  - {name}: {t_s}{mark}{('  # ' + desc) if desc else ''}")
    return "\n".join(rows)

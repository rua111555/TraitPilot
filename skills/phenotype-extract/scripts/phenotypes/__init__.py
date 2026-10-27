# -*- coding: utf-8 -*-
"""phenotypes/__init__.py —— 表型插件注册表

约定: 本目录下每个非 _ 开头的 .py 模块, 若定义了顶层 dict `TASK`, 即视为一个表型插件。
新增表型 = 在本目录新增一个 .py 模块 + 在数据库 models 表注册对应模型, 无需改主脚本。

TASK 字段:
  name         表型名(大小写不敏感, 供 --pheno 选择), 作为输出文件前缀(trait.lower())
  title        一句话中文标题(列建议表用)
  description  计算方式说明(列建议表用)
  model_names  该表型需要的 DB models.name 列表(存在即算"模型就绪")
  trait        GWAS 表型列名(GAPIT 表头 / PLINK 无表头第三列语义)
  pheno_col    CSV 明细中该表型的列名
  compute      fn(ctx) -> (value, extra_cols dict); ctx 见主脚本
  mask_encode  可选 fn(seg)->uint8 保存预测 mask(默认 255=非背景)
  validate     可选 dict, 供出口校验门禁用。目前支持:
                 value_range: [lo, hi]  该表型的物理值域(如 IMF 为 [0,100])
               插件若声明了它就按它判; 未声明则跳过值域判定(仍做整批检查)。

插件契约(重要): compute 在「算不出可信值」时必须抛 PhenotypeError(见 _common),
  **不得返回兜底数值** —— 兜底值通常落在合法值域内(如抠图失败时 IMF=0.0%),
  会伪装成正常结果一路带进 GWAS, 事后无法从数值上分辨。
"""
# ⚠️ 不要在这里 `from ._common import foreground_mask` —— 那会让任何
#    `import phenotypes` 都连带 `import cv2`（_common 顶层 import cv2），
#    于是 validate.py 声明的"不 import ML 依赖"失效：它以独立 CLI 在 GWAS
#    入口跑时（那边没有 cv2），连加载都做不到。
#    改用惰性再导出（PEP 562）：只在真的取用 `phenotypes.foreground_mask` 时才
#    加载 _common，`import phenotypes` 本身保持零重依赖。
_LAZY = ("foreground_mask", "PhenotypeError")


def __getattr__(name):
    if name in _LAZY:
        from . import _common
        return getattr(_common, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

import os
import importlib

_HERE = os.path.dirname(__file__)
_TASKS = None


def load_tasks():
    """扫描本目录, 返回所有 TASK 插件 dict 列表(按模块名排序)。"""
    global _TASKS
    if _TASKS is not None:
        return _TASKS
    tasks = []
    for fn in sorted(os.listdir(_HERE)):
        if not fn.startswith("_") and fn.endswith(".py"):
            mod = importlib.import_module(f"{__name__}.{fn[:-3]}")
            task = getattr(mod, "TASK", None)
            if isinstance(task, dict) and task.get("compute"):
                tasks.append(task)
    _TASKS = tasks
    return tasks


def get_task(name):
    """按 name(大小写不敏感) 查插件, 返回 TASK dict。"""
    if name is None:
        return None
    key = name.strip().lower()
    for t in load_tasks():
        if str(t.get("name", "")).strip().lower() == key:
            return t
    return None

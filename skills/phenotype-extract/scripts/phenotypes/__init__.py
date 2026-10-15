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
"""
from ._common import meat_mask  # noqa: F401   方便插件复用

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

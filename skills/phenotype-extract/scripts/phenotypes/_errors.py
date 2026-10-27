# -*- coding: utf-8 -*-
"""phenotypes/_errors.py —— 只放异常类，**不 import 任何重依赖**。

为什么单独一个模块：``PhenotypeError`` 需要被两处引用，而这两处的依赖预算
完全不同：

  · ``_common.py`` / 各插件 —— 本来就要 import cv2/numpy，从哪拿都无所谓
  · ``scripts/validate.py`` —— 声明了「不 import 任何 ML 依赖」，因为门禁要能作为
    独立 CLI 在 GWAS 入口跑（那边没有 cv2）

早期把异常类放在 ``_common.py``，于是 ``validate.py`` 一旦
``from phenotypes._common import PhenotypeError`` 就会连带执行 ``import cv2``，
"不依赖 ML 库"的声明直接失效（实测：屏蔽 cv2 后 import validate 即
ModuleNotFoundError）。而**把这个类分散定义两份**也不行 —— 插件抛的与主脚本
捕获的必须是同一个类，否则 ``except`` 对不上、失败被静默漏掉。

所以：类定义放在这里（零依赖），其余模块从这里导入/再导出，类的同一性由
Python 的模块缓存保证。

文件名以 ``_`` 开头，故不会被 ``load_tasks()`` 当成表型插件。
"""


class PhenotypeError(Exception):
    """单样本算不出可信值时抛出。

    插件在「算不出可信值」时必须抛本异常，而不是返回一个兜底的数值：
    兜底值通常落在合法值域内（如 IMF=0.0%），会一路带进 GWAS，事后无法从数值上
    分辨真假。主脚本捕获它并把该样本标成 ``fail:<原因>``。
    """

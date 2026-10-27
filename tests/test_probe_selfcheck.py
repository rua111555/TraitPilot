# -*- coding: utf-8 -*-
"""`tools/concurrency/verify_fix_probe.py` 的**判据自检** —— 防止"复核工具本身是纸面具备"。

背景：本探针是"修了代码但判据是装饰性的"这一风险的对策工具，它自己却栽在同形缺陷上。
第二轮对抗审计实测到两处（均已在探针里修掉，本文件钉住不许回归）：

  · **C 段陈旧变量**：断言写成 `rc == 1 or rc4 == 1`，而 `rc` 是上一段 stage5 的旧值、
    **恒为 1** → `or` 左边永远成立，`rc4` 取什么都不影响结论。把被测的
    `bqsr.coverage` 登记整段删掉（= 那个 critical 缺陷真的回退了），探针仍报
    `[OK] ... 45/45 全绿`。
  · **D 段判据不参与断言**：断言只有 `rc6 == 1`，而该场景下 rc 会被**另一条**判据
    （bqsr.coverage）拉成 1；`reads_conservation_lost` 仅出现在说明字符串里。
    把它的检测块改成 `if False:`，探针照样全绿，输出还自相矛盾地打印
    `rc=1（期望 1）；无 reads_conservation_lost`。

根因与 `test_defect_class_guards.py` 记录的一致：**修复的作用域错了** —— 当时按"这条
断言看起来对"逐个写，而没有按"断言必须能区分"这一**写法**去检查。故这里做静态检查。
"""
import ast
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(REPO, "tools", "concurrency", "verify_fix_probe.py")

# 只关心"校验器调用的结果"这类变量（rc / out / rc4 / out12 …）。
# 其它名字（循环变量、模块名、函数名）不属本条缺陷类，扫进来只会产生噪音。
RESULT_VAR = re.compile(r"^(?:rc|out)\d*$")


def _load():
    with open(PROBE, encoding="utf-8") as f:
        src = f.read()
    return src, src.splitlines(), ast.parse(src)


def _record_calls(tree):
    """按出现顺序返回 record(name, cond, ...) 的 (行号, cond AST 节点)。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "record" \
                and len(node.args) >= 2:
            out.append((node.lineno, node.args[1]))
    return sorted(out, key=lambda t: t[0])


def _result_vars_assigned_between(lines, a, b):
    """第 a..b 行之间被赋值的结果类变量（含 `rc, out = verify(...)` 这种元组解包）。"""
    names = set()
    for i in range(a - 1, min(b - 1, len(lines))):
        for m in re.finditer(r"\b((?:rc|out)\d*)\b(?=\s*(?:,|=))", lines[i]):
            if re.search(rf"\b{re.escape(m.group(1))}\b\s*(?:,\s*\w+\s*)?=[^=]", lines[i]):
                names.add(m.group(1))
    return names


def test_no_stale_result_variable_in_assertions():
    """★C 段的根因：断言引用的结果变量必须在**本段内**被赋值。

    跨段引用拿到的是上一次校验器调用的旧值，断言于是可能恒真（或恒假）而不自知 ——
    这正是"判据看着在、实际什么也没断言"。段边界取相邻两个 record() 调用。
    """
    src, lines, tree = _load()
    offenders = []
    prev = 0
    for ln, cond in _record_calls(tree):
        used = {n.id for n in ast.walk(cond)
                if isinstance(n, ast.Name) and RESULT_VAR.match(n.id)}
        stale = used - _result_vars_assigned_between(lines, prev, ln)
        if stale:
            offenders.append(f"line {ln}: {sorted(stale)} —— {lines[ln-1].strip()[:90]}")
        prev = ln
    assert not offenders, (
        "探针断言引用了本段未赋值的结果变量（陈旧值 ⇒ 断言可能恒真）：\n  "
        + "\n  ".join(offenders))


def test_no_substring_only_assertion_on_verifier_output():
    """★D 段的根因：不得用 `"判据名" in out` 这类**子串**断言代替判据本身。

    子串断言无法区分三种情形：判据以 hard+FAIL 登记（真拦下）、以 warn 登记（放行了）、
    或只是恰好出现在说明文字里。判据整段消失时子串仍可能成立 —— 于是"删掉检测代码"
    这样的真实回退**测不出来**。判断"某判据是否真的硬拦"必须读 `.verify/<stage>.json`。

    规则精确到**"子串不得是唯一依据"**：同一个条件里若已有结构化判据
    （`hard_failed(...)` / `verdicts(...) == (...)`），子串可以并存 —— 那时它断言的是
    **另一件事**（消息内容是否说清了失败原因），由结构化判据兜底真伪。
    例如 O 段既要求 `combined.exists` 是 hard+FAIL，又要求文案把"0 字节"与"完全没生成"
    区分开：后者只有子串能表达，但它不再是"该判据是否生效"的依据。

    允许的例外：文档内容检查（读的是 md 文本，不是校验器 stdout）。
    """
    src, lines, tree = _load()
    offenders = []
    for ln, cond in _record_calls(tree):
        has_substring = False
        has_structured = bool(re.search(r"hard_failed\(|verdicts\(|_v\d+ ==|_v\d+\.",
                                        ast.unparse(cond)))
        for cmp_node in ast.walk(cond):
            if not isinstance(cmp_node, ast.Compare):
                continue
            left = cmp_node.left
            if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                    and any(isinstance(op, ast.In) for op in cmp_node.ops)
                    and any(isinstance(c, ast.Name) and re.match(r"^out\d*$", c.id)
                            for c in cmp_node.comparators)):
                has_substring = True
        if has_substring and not has_structured:
            offenders.append(f"line {ln}: {lines[ln-1].strip()[:100]}")
    assert not offenders, (
        "探针把 stdout 子串当作**唯一**依据（判据消失时仍可能通过）—— "
        "请加 verdicts()/hard_failed() 读 `.verify/<stage>.json`：\n  "
        + "\n  ".join(offenders))


def test_structured_helpers_exist_and_are_used():
    """前提自检：上面两条防线的"正解"必须真的存在于探针里，否则只是禁止而无出路。"""
    src, _, _ = _load()
    assert "def verdicts(" in src, "探针缺少 verdicts() 结构化读回助手"
    assert "def hard_failed(" in src, "探针缺少 hard_failed() 判据真值助手"
    assert "hard_failed(" in src.split("def hard_failed(")[1], (
        "hard_failed() 定义了却没人调用 —— 防线的正解形同虚设")


def test_probe_still_reports_both_sections_c_and_d():
    """前提自检：被审计点名的 C/D 两段仍在（别用"删掉该段"的方式让上面两条变绿）。"""
    src, lines, _ = _load()
    found_c = any("no BQSR at all" in l for l in lines)
    found_d = any("one sample lost" in l for l in lines)
    assert found_c and found_d, (
        f"C 段存在={found_c}，D 段存在={found_d} —— 两段是被审计确认过的真实缺陷判据，"
        f"不得为了让本文件变绿而删除")

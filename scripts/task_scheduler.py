#!/usr/bin/env python3
"""task_scheduler.py — 一个轻量的任务依赖调度器（DAG 拓扑执行）。

功能概览：
  * 用有向无环图 (DAG) 描述任务及其依赖关系
  * 检测环依赖并报错
  * 支持串行 / 并行（基于线程池，按层推进）两种调度模式
  * 支持任务重试、超时、执行耗时统计
  * 支持导出 DAG 的 Graphviz DOT 描述

示例：
    python task_scheduler.py --demo --workers 4
"""

from __future__ import annotations

import argparse
import logging
import random
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scheduler")


class CycleError(ValueError):
    """依赖图中存在环。"""


class TaskFailed(RuntimeError):
    """任务在执行阶段抛出且重试耗尽。"""


@dataclass
class Task:
    """一个可执行任务。

    name        : 唯一标识
    func        : 无参可调用对象，返回值会被记录
    deps        : 依赖的任务名集合
    retries     : 失败后的最大重试次数（不含首次）
    timeout     : 单次执行超时（秒），None 表示不限
    """

    name: str
    func: Callable[[], object]
    deps: Set[str] = field(default_factory=set)
    retries: int = 0
    timeout: Optional[float] = None

    # 运行期状态
    state: str = "pending"          # pending|running|done|failed
    result: object = None
    error: Optional[BaseException] = None
    attempts: int = 0
    elapsed: float = 0.0


class Scheduler:
    """按 DAG 拓扑序调度并执行任务。"""

    def __init__(self, tasks: Optional[List[Task]] = None) -> None:
        self._tasks: Dict[str, Task] = {}
        self._children: Dict[str, Set[str]] = defaultdict(set)
        self._lock = threading.Lock()
        if tasks:
            for t in tasks:
                self.add_task(t)

    # ---------- 图构建 ----------

    def add_task(self, task: Task) -> "Scheduler":
        if task.name in self._tasks:
            raise ValueError(f"重复的任务名: {task.name}")
        self._tasks[task.name] = task
        for dep in task.deps:
            self._children[dep].add(task.name)
        return self

    def validate(self) -> None:
        """检查所有依赖存在且图中无环。"""
        for t in self._tasks.values():
            for dep in t.deps:
                if dep not in self._tasks:
                    raise KeyError(f"任务 {t.name} 依赖了不存在的任务 {dep}")
        order = self.topo_order()  # 会触发环检测
        log.info("依赖校验通过，共 %d 个任务，拓扑深度 %d", len(order), len(order))

    def topo_order(self) -> List[str]:
        """Kahn 算法求拓扑序；发现环则抛 CycleError。"""
        indeg = {name: len(t.deps) for name, t in self._tasks.items()}
        queue = deque(sorted(n for n, d in indeg.items() if d == 0))
        order: List[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for child in sorted(self._children[node]):
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
        if len(order) != len(self._tasks):
            stuck = sorted(set(self._tasks) - set(order))
            raise CycleError(f"检测到环依赖，涉及任务: {stuck}")
        return order

    def levels(self) -> List[List[str]]:
        """按层（可并行批）划分任务。"""
        indeg = {name: len(t.deps) for name, t in self._tasks.items()}
        current = sorted(n for n, d in indeg.items() if d == 0)
        result: List[List[str]] = []
        while current:
            result.append(current)
            nxt: List[str] = []
            for node in current:
                for child in sorted(self._children[node]):
                    indeg[child] -= 1
                    if indeg[child] == 0:
                        nxt.append(child)
            current = sorted(nxt)
        return result

    # ---------- 执行 ----------

    def _run_one(self, task: Task) -> None:
        """执行单个任务，含重试与超时控制。"""
        started = time.perf_counter()
        for attempt in range(task.retries + 1):
            task.attempts = attempt + 1
            try:
                if task.timeout is None:
                    task.result = task.func()
                else:
                    self._run_with_timeout(task)
                task.state = "done"
                break
            except BaseException as exc:  # noqa: BLE001 - 需捕获并记录
                task.error = exc
                log.warning(
                    "任务 %s 第 %d 次执行失败: %s", task.name, attempt + 1, exc
                )
                if attempt == task.retries:
                    task.state = "failed"
        task.elapsed = time.perf_counter() - started
        if task.state == "failed":
            raise TaskFailed(f"任务 {task.name} 最终失败") from task.error

    @staticmethod
    def _run_with_timeout(task: Task) -> None:
        """在子线程中执行以支持超时（父线程 join 计时）。"""
        box: Dict[str, object] = {}

        def worker() -> None:
            try:
                box["value"] = task.func()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        th.join(task.timeout)
        if th.is_alive():
            raise TimeoutError(f"任务超时 (> {task.timeout}s)")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        task.result = box.get("value")

    def run(self, workers: int = 1) -> Dict[str, Task]:
        """按层执行全部任务。workers>1 时层内并行。"""
        self.validate()
        for layer in self.levels():
            if workers <= 1:
                for name in layer:
                    self._run_one(self._tasks[name])
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(self._run_one, self._tasks[n]): n for n in layer
                    }
                    for fut in as_completed(futures):
                        fut.result()  # 让失败向上抛出
        return self._tasks

    # ---------- 输出与统计 ----------

    def summary(self) -> str:
        lines = ["", "任务执行汇总", "-" * 46]
        for name in self.topo_order():
            t = self._tasks[name]
            mark = {"done": "✓", "failed": "✗", "pending": "·"}.get(t.state, "?")
            lines.append(
                f"  {mark} {name:<14} state={t.state:<8} "
                f"attempts={t.attempts} elapsed={t.elapsed:.3f}s"
            )
        done = sum(1 for t in self._tasks.values() if t.state == "done")
        total = sum(t.elapsed for t in self._tasks.values())
        lines.append("-" * 46)
        lines.append(f"  成功 {done}/{len(self._tasks)}，累计耗时 {total:.3f}s")
        return "\n".join(lines)

    def to_dot(self) -> str:
        """导出 Graphviz DOT。"""
        out = ["digraph tasks {", "  rankdir=LR;"]
        for t in self._tasks.values():
            out.append(f'  "{t.name}";')
        for name, t in self._tasks.items():
            for dep in sorted(t.deps):
                out.append(f'  "{dep}" -> "{name}";')
        out.append("}")
        return "\n".join(out)


# ---------------- 演示用任务工厂 ----------------

def make_demo() -> Scheduler:
    """构造一个演示 DAG：数据准备 -> 特征工程 -> 训练/评估 -> 汇总。"""
    rng = random.Random(42)

    def work(tag: str, lo: float = 0.2, hi: float = 0.8) -> Callable[[], str]:
        def _inner() -> str:
            time.sleep(rng.uniform(lo, hi))
            if tag == "flaky" and rng.random() < 0.5:
                raise RuntimeError("偶发失败")
            return f"{tag}-ok"
        return _inner

    sched = Scheduler()
    sched.add_task(Task("load", work("load", 0.1, 0.3)))
    sched.add_task(Task("clean", work("clean"), deps={"load"}))
    sched.add_task(Task("feat_a", work("feat_a"), deps={"clean"}))
    sched.add_task(Task("feat_b", work("feat_b"), deps={"clean"}))
    sched.add_task(Task("train", work("flaky", 0.3, 1.0), deps={"feat_a", "feat_b"}, retries=3))
    sched.add_task(Task("eval", work("eval"), deps={"train"}))
    sched.add_task(Task("report", work("report", 0.1, 0.2), deps={"eval"}))
    return sched


def main() -> int:
    ap = argparse.ArgumentParser(description="DAG 任务调度器 Demo")
    ap.add_argument("--demo", action="store_true", help="运行内置演示 DAG")
    ap.add_argument("--workers", type=int, default=1, help="并行度")
    ap.add_argument("--dot", metavar="FILE", help="导出 DOT 到文件")
    args = ap.parse_args()

    sched = make_demo() if args.demo else Scheduler()
    if not sched._tasks:
        log.error("没有任务可执行，请使用 --demo")
        return 2

    if args.dot:
        with open(args.dot, "w", encoding="utf-8") as fh:
            fh.write(sched.to_dot())
        log.info("DOT 已写入 %s", args.dot)

    try:
        sched.run(workers=args.workers)
    except (TaskFailed, CycleError) as exc:
        log.error("调度中止: %s", exc)
    print(sched.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

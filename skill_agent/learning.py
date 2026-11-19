"""项目内的失败记录：记录观察事实，只有相同调用的新脚本版本通过后才收录修复经验。"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path


DIAGNOSTICS = (
    ("python-syntax", r"(?:Syntax|Indentation|Tab)Error:", "先检查报错行与上一行的缩进、括号和引号。"),
    ("bash-syntax", r"syntax error near|unexpected (?:EOF|end of file)", "先对生成的脚本做 bash -n 语法检查。"),
    ("missing-module", r"ModuleNotFoundError:|ImportError:", "核对运行脚本的解释器和模块安装环境。"),
    ("missing-input", r"No such file or directory|FileNotFoundError:", "核对输入文件和任务的工作目录。"),
    ("bad-arguments", r"unrecognized arguments:|invalid option|unknown option", "对照当前工具版本检查参数。"),
    ("data-shape", r"(?:Key|Index|Value|Type)Error:", "用小输入核对字段、类型和边界条件。"),
)
TIPS = {kind: tip for kind, _, tip in DIAGNOSTICS}
TIPS.update(timeout="检查耗时和资源需求，长任务可改用后台运行。", runtime="查看失败日志，修正后用同一调用核验。",
            edit_match="重新读取文件，用当前版本中唯一匹配的文本定位修改。",
            file_io="核对目标文件、父目录和可写空间。", launch="检查任务启动日志和 PID 文件。",
            draft_syntax="草稿未通过语法预检；按解析器指出的位置修正，再提交修改。")
MAX_SOURCE = 1024 * 1024
PY_CHECKER = f"python:{sys.version_info.major}.{sys.version_info.minor}"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def category(output):
    return next((kind for kind, pattern, _ in DIAGNOSTICS if re.search(pattern, output)), "runtime")


def signature(output, code):
    exceptions = re.findall(r"(?m)^([A-Za-z_][A-Za-z_0-9]{0,48}(?:Error|Exception)):", output)
    if exceptions:
        return exceptions[-1]
    last = next((line.strip() for line in reversed(output.splitlines())
                 if line.strip() and "warning: setlocale:" not in line), "")
    normalized = re.sub(r"""'[^']*'|"[^"]*"|/[^\s]+|\d+""", "?", last)
    return f"exit:{code}" + (":" + digest(normalized.encode())[:12] if normalized else "")


def _summary(text):
    text = " ".join(str(text or "").split())
    if len(text) > 180 or re.search(r"https?://|[/\\`$]|(?:token|password|secret|api[_-]?key)\s*[:=]|[A-Za-z0-9_-]{32,}", text, re.I):
        return ""
    return text


class ErrorLearning:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.path = self.project / ".skill-agent" / "learning.sqlite3"

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT, invocation TEXT, target TEXT, kind TEXT, failures INTEGER,
                recoveries INTEGER DEFAULT 0, revision TEXT, generation INTEGER, seen REAL,
                repair TEXT DEFAULT '', script TEXT, last_exit INTEGER, checker TEXT, signature TEXT NOT NULL,
                UNIQUE(invocation, kind, signature))""")
            db.execute("CREATE INDEX IF NOT EXISTS by_target ON lessons(target, seen)")
            db.execute("CREATE TABLE IF NOT EXISTS deliveries (job TEXT PRIMARY KEY)")
            yield db

    def snapshot(self, cwd, command, backend, permissions, repair=""):
        attempt = {"invocation": digest(f"{cwd}\0{backend}\0{command}".encode()), "target": "",
                   "revision": "", "previous": None, "project": digest(str(self.project).encode())}
        try:
            words = shlex.split(command)
            if len(words) < 2 or re.search(r"[;&|<>`$*?\[\n]", command):
                return attempt
            exe = Path(words[0]).name
            path = (Path(cwd) / words[1]).resolve()
            if not ((re.fullmatch(r"python(?:3(?:\.\d+)?)?", exe) and path.suffix == ".py") or
                    (exe == "bash" and path.suffix in {".sh", ".bash"})):
                return attempt
            if not path.is_relative_to(self.project) or words[1].startswith("-"):
                return attempt
            read = {"file_path": str(path)}
            if (permissions.check_path(path) or permissions.is_denied("Read", read)
                    or permissions.needs_confirm("Read", read)[0]):
                return attempt
            attempt.update(target=self._target(path), script=path.name[:100])
            if backend in {"host", "rlimit"} and not words[0].startswith("."):
                interpreter = shutil.which(words[0])
                expected = "/bin/bash" if exe == "bash" else sys.executable
                if interpreter and Path(interpreter).samefile(expected):
                    attempt["checker"] = "bash" if exe == "bash" else PY_CHECKER
            if not path.is_file() or path.stat().st_size > MAX_SOURCE:
                return attempt
            with path.open("rb") as stream:
                source = stream.read(MAX_SOURCE + 1)
            if len(source) > MAX_SOURCE:
                return attempt
            attempt["revision"] = digest(source)
            if self.path.is_file():
                with self._db() as db:
                    row = db.execute("SELECT id,generation,revision FROM lessons WHERE invocation=? "
                                     "ORDER BY seen DESC,id DESC LIMIT 1", (attempt["invocation"],)).fetchone()
                    if row and row["revision"]:
                        attempt["previous"] = [row["id"], row["generation"]]
                        attempt["changed"] = row["revision"] != attempt["revision"]
                        if attempt["changed"]:
                            attempt["repair"] = _summary(repair)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            logging.getLogger(__name__).debug("Learning snapshot unavailable: %s", type(exc).__name__)
        return attempt

    def _target(self, path):
        return digest(str(path.relative_to(self.project)).encode())

    def observe(self, attempt, kind=None, job=None, exit_code=None, signal=""):
        if not kind and not attempt.get("previous"):
            return ""
        try:
            with self._db() as db:
                if job and not db.execute("INSERT OR IGNORE INTO deliveries VALUES (?)", (job,)).rowcount:
                    return ""
                if kind:
                    db.execute("""INSERT INTO lessons(invocation,target,kind,failures,revision,generation,seen,script,last_exit,checker,signature)
                        VALUES (?,?,?,1,?,1,?,?,?,?,?) ON CONFLICT(invocation,kind,signature) DO UPDATE SET
                        failures=failures+1, revision=excluded.revision, generation=generation+1,
                        seen=excluded.seen,last_exit=excluded.last_exit,checker=excluded.checker""",
                               (attempt["invocation"], attempt["target"], kind, attempt["revision"], time.time(),
                                attempt.get("script"), exit_code, attempt.get("checker"), signal))
                    row = db.execute("SELECT id,failures,repair FROM lessons WHERE invocation=? AND kind=? AND signature=?",
                                     (attempt["invocation"], kind, signal)).fetchone()
                    return f"已记录错误 #{row['id']}（{kind}，第 {row['failures']} 次）。{TIPS[kind]}" + self._prior(row)
                different = int(bool(attempt.get("changed")))
                updated = db.execute("UPDATE lessons SET recoveries=recoveries+?, revision='', "
                                     "repair=CASE WHEN ? THEN ? ELSE repair END "
                                     "WHERE id=? AND generation=? AND revision!='' AND invocation=? "
                                     "AND id=(SELECT id FROM lessons WHERE invocation=? ORDER BY seen DESC,id DESC LIMIT 1)",
                                     (different, different, attempt.get("repair", ""), *attempt["previous"],
                                      attempt["invocation"], attempt["invocation"])).rowcount
                if updated and different:
                    return "已记录修订后的脚本在相同调用下退出码为 0；产物仍需单独验证。"
        except (OSError, sqlite3.Error) as exc:
            logging.getLogger(__name__).warning("Cannot save error learning: %s", type(exc).__name__)
        return ""

    def file_error(self, path, tool, kind):
        if not path.is_relative_to(self.project):
            return ""
        key = self._target(path)
        return self.observe({"invocation": digest((tool + key).encode()), "target": key,
                             "revision": "", "script": path.name[:100]}, kind)

    def background(self, task, status, tasks_root):
        attempt = task.get("learning")
        if (not isinstance(attempt, dict) or not {"invocation", "target", "revision"} <= attempt.keys()
                or attempt.get("project") != digest(str(self.project).encode())):
            return ""
        job = task["task_id"]
        if status in {"no_pid", "bad_pid"}:
            return self.observe(attempt, "launch", job)
        if not status.startswith("exit:"):
            return ""
        code = int(status.split(":")[1])
        output = ""
        if code and re.fullmatch(r"[a-f0-9]{10}", job):
            try:
                log = Path(tasks_root) / job / "output.log"
                if log.is_file() and log.resolve() == Path(tasks_root).resolve() / job / "output.log":
                    with log.open("rb") as stream:
                        stream.seek(max(0, stream.seek(0, 2) - 8192))
                        output = stream.read(8192).decode(errors="replace")
            except OSError:
                pass
        return self.observe(attempt, category(output) if code else None, job, code, signature(output, code))

    def for_file(self, path, kind=None, checker=None):
        if not self.path.is_file() or not path.is_relative_to(self.project):
            return []
        try:
            with self._db() as db:
                return db.execute("SELECT * FROM lessons WHERE target=? ORDER BY "
                                  "(kind=? AND checker=?) DESC,seen DESC LIMIT 5",
                                  (self._target(path), kind, checker)).fetchall()
        except (OSError, sqlite3.Error):
            return []

    @staticmethod
    def _prior(row):
        return ("\n历史修复说明（仅供核对）：" + row["repair"]) if row["repair"] else ""

    def advice(self, rows):
        return "\n".join(f"#{row['id']} {row['kind']}：{TIPS[row['kind']]}" + self._prior(row)
                         for row in rows if row["kind"] in TIPS)

    def check_script(self, path, source):
        kind = {".py": "python-syntax", ".sh": "bash-syntax", ".bash": "bash-syntax"}.get(path.suffix)
        checker = PY_CHECKER if kind == "python-syntax" else "bash"
        rows = self.for_file(path, kind, checker)
        notes = self.advice(rows)
        if len(source) > MAX_SOURCE or not any(row["kind"] == kind and row["checker"] == checker for row in rows):
            return "", notes
        argv = ([sys.executable, "-I", "-S", "-c",
                 "import sys; compile(sys.stdin.read(), '<script>', 'exec')"]
                if kind == "python-syntax" else ["/bin/bash", "--noprofile", "--norc", "-n"])
        try:
            result = subprocess.run(argv, input=source.encode(), capture_output=True, timeout=3,
                                    env={"LC_ALL": "C", "PYTHONIOENCODING": "utf-8"})
            if result.returncode > 0:
                self.file_error(path, "语法预检", "draft_syntax")
                return ("该脚本曾出现语法错误；本次语法预检未通过，文件未修改。请修正后再写入。\n"
                        + result.stderr.decode(errors="replace")[-800:]), notes
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "", notes

    def prevention(self):
        if not self.path.is_file():
            return ""
        try:
            with self._db() as db:
                rows = db.execute("SELECT kind,SUM(failures) AS n FROM lessons GROUP BY kind "
                                  "ORDER BY n DESC, MAX(seen) DESC LIMIT 3")
                return "\n".join(f"- {TIPS[row['kind']]}（本项目曾遇到 {row['n']} 次）"
                                 for row in rows if row["kind"] in TIPS)
        except (OSError, sqlite3.Error):
            return ""

    def rows(self, limit=-1):
        if not self.path.is_file():
            return []
        with self._db() as db:
            return db.execute("SELECT * FROM lessons ORDER BY seen DESC LIMIT ?", (limit,)).fetchall()

    def describe(self, limit=20):
        try:
            rows = self.rows(limit)
        except (OSError, sqlite3.Error):
            return "暂时无法读取本项目的错误记录。"
        lines = []
        for row in rows:
            code = f"，退出码 {row['last_exit']}" if row["last_exit"] is not None else ""
            signal = (" · " + row["signature"]) if row["signature"] else ""
            lines.append(f"#{row['id']} {row['script'] or 'Bash'} · {row['kind']}{signal}：失败 {row['failures']} 次{code}，"
                         f"修订后同一调用通过 {row['recoveries']} 次。"
                         + (self._prior(row) or TIPS.get(row["kind"], "")))
        return "\n\n".join(lines) or "本项目还没有错误记录。"

    def forget(self, lesson_id):
        if not self.path.is_file():
            return False
        with self._db() as db:
            return bool(db.execute("DELETE FROM lessons WHERE id=?", (lesson_id,)).rowcount)


def main():
    parser = argparse.ArgumentParser(description="查看本项目的错误学习记录")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--forget", type=int, metavar="ID", help="撤销指定编号的错误记录及修复经验")
    args = parser.parse_args()
    learning = ErrorLearning(args.project)
    if args.forget is not None:
        print("已撤销。" if learning.forget(args.forget) else "未找到该编号。")
        return
    print(learning.describe(limit=-1))


if __name__ == "__main__":
    main()

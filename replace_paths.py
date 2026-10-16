#!/usr/bin/env python3
"""
WGS Skill 迁移工具 — 路径替换 + 数据库建表
===========================================

功能：
  1. 一键替换 SKILL.md 中所有 conda 工具绝对路径
  2. 一键替换 SKILL 中所有数据基目录路径
  3. 在指定路径创建空的 pipeline.db（仅建表，不插入数据）

用法：
  # 仅替换 conda 路径
  python3 replace_paths.py --conda /home/newuser/miniconda3

  # 替换 conda 路径 + 数据路径
  python3 replace_paths.py --conda /home/newuser/miniconda3 --data /home/data/Pig_geno

  # 仅替换数据路径
  python3 replace_paths.py --data /home/data/Pig_geno

  # 替换路径 + 创建数据库
  python3 replace_paths.py --conda /home/newuser/miniconda3 --data /home/data/Pig_geno --db /home/data/Pig_geno/pipeline.db

  # 仅创建数据库
  python3 replace_paths.py --db /home/data/Pig_geno/pipeline.db --create-db-only

  # 预览（不实际修改）
  python3 replace_paths.py --conda /home/newuser/miniconda3 --data /home/data/Pig_geno --dry-run
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# ── 默认值 ─────────────────────────────────────────────────────────
OLD_CONDA_ROOT = "/home/chenyuxi/anaconda3"
OLD_DATA_ROOT = "/data1/chenyx/Pig_geno"

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_genome (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    description      TEXT,
    fasta_path       TEXT NOT NULL,
    dict_path        TEXT,
    fai_path         TEXT,
    bwa_index_prefix TEXT,
    species          TEXT,
    build            TEXT,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fastq_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_number   INTEGER NOT NULL,
    sample_name     TEXT NOT NULL,
    fastq_dir       TEXT NOT NULL,
    read1_path      TEXT NOT NULL,
    read2_path      TEXT NOT NULL,
    file_type       TEXT DEFAULT 'WGS paired-end',
    read_length     INTEGER,
    file_size_bytes INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT NOT NULL UNIQUE,
    output_prefix     TEXT NOT NULL,
    sample_list       TEXT NOT NULL,
    thread_count      INTEGER DEFAULT 40,
    status            TEXT DEFAULT 'pending',
    started_at        TIMESTAMP,
    completed_at      TIMESTAMP,
    total_duration_sec REAL,
    error_message     TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sample_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    sample_name     TEXT NOT NULL,
    step_name       TEXT NOT NULL,
    output_path     TEXT,
    status          TEXT DEFAULT 'pending',
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    duration_sec    REAL,
    error_message   TEXT,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
);

CREATE TABLE IF NOT EXISTS pipeline_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# ── 路径替换 ──────────────────────────────────────────────────────

def find_skill_dir() -> Path:
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir / "agent" / "skills",           # 仓库内聚后的位置（优先）
        script_dir / "skills",
        script_dir / ".claude" / "skills",         # legacy（Claude Code 布局）
        script_dir.parent / "agent" / "skills",
        script_dir.parent / "skills",
        script_dir.parent / ".claude" / "skills",
        Path.cwd() / "skills",
        Path.cwd() / ".claude" / "skills",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    print("错误: 找不到 skills/ 目录", file=sys.stderr)
    print("请在仓库（含 agent/skills/ 或 .claude/skills/）下运行此脚本", file=sys.stderr)
    sys.exit(1)


def get_all_affected_files(skill_dir: Path) -> list[Path]:
    """获取所有 .md / .py 文件"""
    files = []
    for ext in ("*.md", "*.py"):
        files.extend(skill_dir.rglob(ext))
    return sorted(files)


def count_occurrences(files: list[Path], old_text: str) -> list[tuple[Path, int]]:
    result = []
    for f in files:
        count = f.read_text(encoding="utf-8").count(old_text)
        if count > 0:
            result.append((f, count))
    return result


def do_replace(files: list[Path], old_text: str, new_text: str, label: str) -> int:
    """执行替换，返回修改的文件数"""
    affected = count_occurrences(files, old_text)

    if not affected:
        print(f"未找到包含「{label}」的文件，跳过")
        return 0

    print(f"\n将替换「{old_text}」→「{new_text}」")
    print(f"涉及 {len(affected)} 个文件:")
    print("-" * 60)
    for f, count in affected:
        rel = f.relative_to(files[0].parent).parent if len(files) > 0 else f.name
        print(f"  {f.relative_to(files[0].parent.parent.parent)}  ({count} 处)")
    print()

    for f, _ in affected:
        content = f.read_text(encoding="utf-8")
        content = content.replace(old_text, new_text)
        f.write_text(content, encoding="utf-8")
        print(f"  ✅ {f.relative_to(files[0].parent.parent.parent)}")

    return len(affected)


def replace_paths(
    skill_dir: Path,
    new_conda_root: str | None,
    new_data_root: str | None,
    dry_run: bool = False,
) -> None:
    files = get_all_affected_files(skill_dir)

    print("=" * 60)
    print("📝 路径替换")
    print("=" * 60)

    conda_affected = []
    data_affected = []

    if new_conda_root:
        conda_affected = count_occurrences(files, OLD_CONDA_ROOT)
        print(f"\n旧 conda 路径: {OLD_CONDA_ROOT}")
        print(f"新 conda 路径: {new_conda_root}")
        if conda_affected:
            print(f"涉及 {len(conda_affected)} 个文件:")
            for f, count in conda_affected:
                print(f"  {f.relative_to(skill_dir.parent.parent)}  ({count} 处)")
        else:
            print("（无涉及文件）")
    else:
        print("\n⏭️  跳过 conda 路径替换（未提供 --conda）")

    if new_data_root:
        data_affected = count_occurrences(files, OLD_DATA_ROOT)
        print(f"\n旧数据路径: {OLD_DATA_ROOT}")
        print(f"新数据路径: {new_data_root}")
        if data_affected:
            print(f"涉及 {len(data_affected)} 个文件:")
            for f, count in data_affected:
                print(f"  {f.relative_to(skill_dir.parent.parent)}  ({count} 处)")
        else:
            print("（无涉及文件）")
    else:
        print("\n⏭️  跳过数据路径替换（未提供 --data）")

    if not conda_affected and not data_affected:
        print("\n无任何需要替换的内容")
        return

    if dry_run:
        print("\n🔍 DRY RUN 模式 — 未执行实际修改")
        return

    print()
    resp = input("确认执行替换？(y/N): ").strip()
    if not resp or resp.lower() not in ("y", "yes"):
        print("已取消")
        return

    if new_conda_root and conda_affected:
        print(f"\n正在替换 conda 路径...")
        n = do_replace(files, OLD_CONDA_ROOT, new_conda_root, "conda 路径")

    if new_data_root and data_affected:
        print(f"\n正在替换数据路径...")
        n = do_replace(files, OLD_DATA_ROOT, new_data_root, "数据路径")

    print(f"\n✅ 替换完成！")


def verify_replacement(skill_dir: Path, new_conda_root: str | None, new_data_root: str | None) -> None:
    files = get_all_affected_files(skill_dir)
    has_issue = False

    print("\n" + "=" * 60)
    print("🔍 验证结果")
    print("=" * 60)

    if new_conda_root:
        remaining = [(f, f.read_text(encoding="utf-8").count(OLD_CONDA_ROOT)) for f in files]
        remaining = [(f, c) for f, c in remaining if c > 0]
        if remaining:
            print(f"\n⚠️  仍有残留旧 conda 路径:")
            for f, c in remaining:
                print(f"  {f.relative_to(skill_dir.parent.parent)}  ({c} 处)")
            has_issue = True
        else:
            print(f"\n✅ conda 路径: 无残留旧路径")

    if new_data_root:
        remaining = [(f, f.read_text(encoding="utf-8").count(OLD_DATA_ROOT)) for f in files]
        remaining = [(f, c) for f, c in remaining if c > 0]
        if remaining:
            print(f"\n⚠️  仍有残留旧数据路径:")
            for f, c in remaining:
                print(f"  {f.relative_to(skill_dir.parent.parent)}  ({c} 处)")
            has_issue = True
        else:
            print(f"\n✅ 数据路径: 无残留旧路径")


# ── 数据库建表 ────────────────────────────────────────────────────

def create_database(db_path: str) -> None:
    path = Path(db_path).resolve()

    if path.exists():
        resp = input(f"\n⚠️  数据库已存在: {path}\n   覆盖重建？(y/N): ").strip()
        if not resp or resp.lower() not in ("y", "yes"):
            print("跳过数据库创建")
            return

    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        conn.executescript(DB_SCHEMA)
        conn.commit()
        print(f"\n✅ 数据库已创建: {path}")
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        print(f"   包含表: {', '.join(t[0] for t in tables)}")
    except sqlite3.Error as e:
        print(f"\n❌ 数据库错误: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


# ── 主入口 ────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="WGS Skill 迁移工具 — 路径替换 + 数据库建表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""使用示例:
  # 同时替换 conda 路径和数据路径
  python3 replace_paths.py --conda /home/newuser/miniconda3 --data /home/data/Pig_geno

  # 仅替换 conda 路径
  python3 replace_paths.py --conda /home/newuser/miniconda3

  # 仅替换数据路径
  python3 replace_paths.py --data /home/data/Pig_geno

  # 替换路径 + 创建数据库
  python3 replace_paths.py --conda /home/newuser/miniconda3 --data /home/data/Pig_geno --db /home/data/Pig_geno/pipeline.db

  # 仅创建数据库
  python3 replace_paths.py --db /home/data/Pig_geno/pipeline.db --create-db-only

  # 预览
  python3 replace_paths.py --conda /home/newuser/miniconda3 --data /home/data/Pig_geno --dry-run
""",
    )

    parser.add_argument("--conda", help="新 conda 根路径，如 /home/newuser/miniconda3")
    parser.add_argument("--data", help="新数据基目录，如 /home/data/Pig_geno")
    parser.add_argument("--db", default="", help="SQLite 数据库路径，如 /data1/chenyx/Pig_geno/pipeline.db")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不实际执行替换")
    parser.add_argument("--create-db-only", action="store_true", help="仅创建数据库，不做路径替换")

    args = parser.parse_args()

    # ── 仅创建数据库 ──
    if args.create_db_only:
        if not args.db:
            parser.error("--create-db-only 需要 --db 参数")
        print("=" * 60)
        print("🗄️  数据库建表")
        print("=" * 60)
        create_database(args.db)
        return

    # ── 路径替换 ──
    if not args.conda and not args.data:
        parser.error("请提供 --conda 和/或 --data，或使用 --create-db-only")

    skill_dir = find_skill_dir()

    replace_paths(skill_dir, args.conda, args.data, dry_run=args.dry_run)

    if not args.dry_run and (args.conda or args.data):
        verify_replacement(skill_dir, args.conda, args.data)

    if args.db:
        print("\n" + "=" * 60)
        create_database(args.db)

    print("\n" + "=" * 60)
    print("✅ 迁移完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()

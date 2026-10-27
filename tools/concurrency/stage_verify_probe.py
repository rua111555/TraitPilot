"""G10 判据：阶段结果校验的**文档承诺**必须与**可执行实现**对得上。

背景（用户诉求逐字）：
  「在WGS管线或者表型提取的每一步结束之后，需要对结果值进行校验，以免出现中途错误
    还传递到后面步骤」

这条诉求的本质是**可执行性**问题，不是文风问题。历史缺陷形态：

  · 阶段脚本收尾写 `wait "${pid}" || log_error ...` —— `||` 把退出码变成 0，
    `set -e` 不触发，脚本继续走到末尾**无条件**写 `.done`，编排层于是误判"完成"
    并推进下一阶段。中途的错误就这样传到了后面。
  · 文档里已有若干"验证"命令（`samtools view -H | grep @RG`、`bcftools stats | grep TSTV`），
    但它们**只是打印**，没有任何一处会因结果不对而阻止阶段推进。

所以判据必须查「承诺」与「实现」两侧，且**双向**：
  只查一侧会漏掉「文档写了校验、代码里没有」或「代码里有校验、文档没让人调用」。

  G10a 校验器脚本存在且可执行（非空、有 main、有各阶段注册）—— 反例：只有文档描述
  G10b 各阶段 skill 都真的写了「先校验再写 .done」的收尾段 —— 反例：某阶段漏了
  G10c 收尾段写成"未过则写 .failed + exit 1"，而不是只打印警告
  G10d 吞错的写法（`wait ... || true` / `|| log_error` 单独了事）**不再出现**在
       并存样本的收尾位置 —— 这是「中途错误传到后面」的直接成因
  G10e 编排层认得 `.failed` 状态（自动推进与恢复检查都要判它）—— 反例：只判 .done
  G10f verifier_path 进了 run.params.json 契约 —— 缺了阶段脚本就定位不到校验器
  G10g 表型侧：门禁真的会**阻止产出**（不是只打印统计）—— 反例：拦不住全 0 值

跑法（真实工具构造 fixture，不 mock —— 本校验器的两处真 bug 都是靠真实输出才暴露的）：
    PATH=/home/chenyuxi/anaconda3/envs/Agent/bin:$PATH \\
      /home/chenyuxi/anaconda3/bin/python -I tools/concurrency/stage_verify_probe.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# ⚠️ 不要写死绝对路径：本探针常被**复制到别处**做变异测试（把被测代码改坏，
#    确认判据会红）。写死本仓路径时，副本里的探针会去测**原仓**的未变异代码，
#    于是"变异没被捕获"看起来像判据无区分度，实则是根本没测到变异体
#    （实测踩到：在 /home/chenyuxi/mut2 副本里做变异，探针全绿）。
#    改为按本文件位置回溯：tools/concurrency/<file> → 仓库根。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILLS = os.path.join(ROOT, "skills")
PIPE = os.path.join(SKILLS, "wgs-expressive-pipeline")
VERIFIER = os.path.join(PIPE, "scripts", "stage_verify.py")
PHENO_VALIDATE = os.path.join(SKILLS, "phenotype-extract", "scripts", "validate.py")

sys.path.insert(0, os.path.join(PIPE, "scripts"))

# 真导入一次校验器：后面的判据要读它的阶段注册表，也顺带验证它 import 得动
import stage_verify as SV  # noqa: E402

log = []


def ok(name, cond, extra=""):
    log.append(("PASS" if cond else "FAIL") + " " + name)
    print(("  [PASS] " if cond else "  [缺陷] ") + name, flush=True)
    if extra:
        print("         " + extra, flush=True)


def read(p):
    return open(p, encoding="utf-8", errors="replace").read()


def _code_lines(text):
    """只取**可执行**行：剔除 docstring/注释/说明性文本。

    为什么必须剔除：imf.py 的 docstring 里**故意**引用了 `max(meat_px, 1)` 来说明
    「为什么不能用它兜底」。纯文本匹配会把这段说明当成缺陷（假失败）——
    与 skill_doc_consistency_probe.py 里「把自己的警告当成缺陷」是同一类陷阱。
    这里用最简的启发式：跳过三引号块内的行、`#` 注释行、以及行内反引号包裹的引用。
    """
    out, in_doc = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if s.startswith('"""') and in_doc is False and s.endswith('"""') and len(s) > 6:
            continue
        if in_doc:
            continue
        if s.startswith("#"):
            continue
        # 行内把 `xxx` 包起来的引用属于说明，不是真代码
        out.append(re.sub(r"`[^`]*`", "", line))
    return out


# 本管线应受门禁约束的阶段 skill（与 stage_verify.py 注册的 --stage 对应）
STAGE_SKILLS = {
    "wgs-express-fastp-qc": "00_fastp_qc",
    "wgs-express-align-sort": "01_bwa_sort",
    "wgs-express-markdup-bqsr": "02_markdup_bqsr",
    "wgs-express-haplotypecaller": "03_haplotypecaller",
    "wgs-express-joint-genotyping": "04_joint_genotyping",
    "wgs-express-filtering": "05_filtering",
}

print("\nG10a 校验器脚本存在、非空、真的注册了各阶段", flush=True)
ok("G10a-1 stage_verify.py 存在且非空",
   os.path.isfile(VERIFIER) and os.path.getsize(VERIFIER) > 2000,
   VERIFIER)
src = read(VERIFIER) if os.path.isfile(VERIFIER) else ""
registered = set(re.findall(r'@stage\("([^"]+)"\)', src))
ok("G10a-2 注册的阶段名覆盖全部 6 个阶段",
   set(STAGE_SKILLS.values()) <= registered,
   f"注册={sorted(registered)}；缺={sorted(set(STAGE_SKILLS.values()) - registered)}")
ok("G10a-3 有 main() 与退出码约定（0=通过/1=拦截）",
   "def main(" in src and "return 1 if rep.blocked else 0" in src)
def _missing_tool_scenario():
    """真跑一次"缺工具"的场景，返回 (rc, missing_tools, 有无 hard 未过项)。

    ⚠️ 为什么要真跑（第二轮审计，实测确认）：G10a-4 早期是**纯文本**断言
    （`"def need_tool" in src` 加两个"某字符串不在源码里"）。那种写法的病是：
      · 后两个 `not in src` 在 `rep.skip` 被删后**恒为真**（鉴别的对象已不存在）
      · 前半句只问"函数定义在不在"—— 把 `need_tool` 的实现改成 fail-open
        （`self.add(..., "warn", True)`，即"缺工具=跳过"，**正是本条要防的那个漏洞**），
        断言**依旧全绿**。实测确认过这一点。
    判据必须落在行为上：这里用 `PATH` 只留一个空目录起子进程，于是 samtools/bcftools
    都探测不到，再看校验器是拦还是放。

    ⚠️ **必须先把产物造齐**，否则走的是另一条分支：产物不存在时 `have()` 先判
    "文件缺失"就返回了，`need_tool` 根本到不了 —— 那样 rc 照样是 1，
    但拦下来的理由是"文件缺失"而不是"缺工具"，本条就变成了"因为别的原因通过"
    （实测踩到过：第一版没有造 fixture，missing_tools 为空、断言反而看不出问题）。
    故这里造齐 FASTQ 与 JSON，让缺工具成为**唯一**的失败原因。
    """
    d = tempfile.mkdtemp()
    out = os.path.join(d, "out")
    os.makedirs(out, exist_ok=True)
    import gzip as _gz
    for tag in ("clean_1", "clean_2"):
        with _gz.open(os.path.join(out, f"s1.{tag}.fq.gz"), "wt") as f:
            for i in range(100):
                f.write(f"@r{i}\nACGTACGTAC\n+\nIIIIIIIIII\n")
    json.dump({"summary": {"after_filtering": {"total_reads": 200},
                           "before_filtering": {"total_reads": 200}},
               "read1_after_filtering": {"total_reads": 100},
               "read2_after_filtering": {"total_reads": 100}},
              open(os.path.join(out, "s1.fastp.json"), "w"))
    env = dict(os.environ, PATH=tempfile.mkdtemp())      # 空 PATH：所有工具都找不到
    p = subprocess.run([sys.executable, VERIFIER, "--stage", "00_fastp_qc",
                        "--run-dir", d, "--samples", "s1"],
                       capture_output=True, text=True, env=env)
    rep = {}
    vf = os.path.join(d, ".verify", "00_fastp_qc.json")
    if os.path.isfile(vf):
        rep = json.load(open(vf))
    hard_open = [c for c in rep.get("checks", [])
                 if c.get("level") == "hard" and not c.get("passed")
                 and not c.get("skipped")]
    return p.returncode, set(rep.get("missing_tools") or []), hard_open


_rc_mt, _mt, _hard = _missing_tool_scenario()
ok("G10a-4 [缺陷确认] 工具缺失是 fail-closed（不判「跳过=通过」）",
   # 实测漏洞：bcftools 不在 PATH 时，内容是乱码的 VCF 会判**通过**
   # （检查标为 SKIP，而 SKIP 不阻止放行）。这等于说任何没装工具的机器上
   # 整个门禁静默失效，而报告上一切正常 —— 正是要防的"静默错误"本身。
   #
   # ⚠️ 断言的**关键项是 rc**，不是"有没有 hard 未过项"。实测教训：
   #    把 `need_tool` 改成 fail-open（`self.add(..., "warn", True)` —— 正是本条
   #    要防的那个漏洞）后，本场景里 **rc 从 1 变成 0**（门禁静默放行），
   #    而 `missing_tools` 里**照样**列着 gzip、hard 未过项也照样有别的来源
   #    （产物判据）。故只查 missing_tools 或"有无 hard 项"都会漏掉这个变异；
   #    必须查 rc —— 它才是"会不会被拦下"这个问题的答案。
   _rc_mt == 1 and "gzip" in _mt and _hard,
   f"空 PATH（产物造齐）下：rc={_rc_mt}（**期望 1** —— 0 即门禁静默放行）；"
   f"missing_tools={sorted(_mt)}（期望含 gzip）；hard 未过项 {len(_hard)} 条")
ok("G10a-5 [缺陷确认] reads 守恒链真的接通（read_totals 有写有读）",
   # 实测漏洞：Stage 2 读 prev["read_totals"]，但没有任何地方写过这个键 ——
   # 守恒检查永远静默跳过（声称存在、从不生效）。
   '"read_totals": dict(self.read_totals)' in src
   and "rep.read_totals.update(totals)" in src,
   "Report 必须把 read_totals 落进 JSON，且各阶段要真的填它")
ok("G10a-6 [缺陷确认] 无装饰性参数（声明了却从不使用的 CLI 选项）",
   # 实测漏洞：--ref / --expected-reads 曾是纯装饰 —— argparse 里有，
   # 代码里从不使用。传了以为多了层检查，实际没有（"纸面具备"的一种）。
   "--ref" not in src and "--expected-reads" not in src,
   "删掉未使用的参数，避免让人误以为传了就有额外检查")
ok("G10a-7 [缺陷确认] 无空壳检查体（写了注释却啥也不做的检查）",
   # 实测漏洞：曾有一处写着「# 5) 比对率」但函数体是 `pass`，
   # 比对率检查根本不存在，而注释让人以为有。
   not re.search(r"比对率.*\n(?:.*\n){0,3}\s*pass\b", src),
   "比对率应由 mapped_ratio / mapped_rate 判据真实实现")
ok("G10a-8 [缺陷确认] 无连续重复行（编辑残留的不可达分支）",
   # 实测漏洞：改代码时留下过连续两行完全相同的 `except Exception as e: ...`
   # —— 语法合法、py_compile 通过，但第二个 except 永远不可达。
   # 这类残留靠肉眼看 diff 容易漏，用静态检查兜住。
   (lambda lines: not any(
       lines[i].strip() and lines[i].strip() == lines[i + 1].strip()
       and not lines[i].strip().startswith(("#", "|", "-", "*", "`"))
       # 连续两行完全相同的 `fi` / `done` / `esac` 等 shell 收尾符是合法的
       and lines[i].strip() not in ("fi", "done", "esac", "}", ")", "EOF")
       for i in range(len(lines) - 1)))(src.splitlines()),
   "连续重复的可执行行多半是编辑残留；py_compile 抓不到这类逻辑错误")
ok("G10a-9 脚本能编译且各阶段函数都能被导入调用",
   # 光靠文本匹配不够 —— 真编译一次并确认注册表完整。
   # 这能抓住「文本看起来对、但 import 就炸」的情况（如循环导入、语法版本问题）。
   (lambda: (
       __import__("py_compile").compile(VERIFIER, doraise=True),
       len(SV._STAGES) == 6,
   )[-1])(),
   f"已注册阶段 = {sorted(SV._STAGES)}")

print("\nG10b/G10c 各阶段 skill 的收尾段：先校验再写 .done，未过写 .failed", flush=True)
missing_gate, weak_gate, wrong_stage = [], [], []
for skill, stage in STAGE_SKILLS.items():
    p = os.path.join(SKILLS, skill, "SKILL.md")
    if not os.path.isfile(p):
        missing_gate.append(skill)
        continue
    t = read(p)
    # 必须调用校验器，且带上本阶段的 --stage 值
    if "stage_verify.py" not in t and "VERIFIER" not in t:
        missing_gate.append(skill)
        continue
    if f'--stage {stage}' not in t and f"--stage {stage}" not in t:
        wrong_stage.append(f"{skill}(期望 --stage {stage})")
    # 必须真的**拦截**：写 .failed 且 exit 1，而不是只打印
    if ".failed" not in t or not re.search(r"exit 1", t):
        weak_gate.append(skill)
ok("G10b 每个阶段 skill 都写了校验门禁调用",
   not missing_gate, f"缺门禁的阶段={missing_gate}" if missing_gate else
   f"全部 {len(STAGE_SKILLS)} 个阶段均有")
ok("G10b-2 每个阶段用的 --stage 值与校验器注册一致",
   not wrong_stage, f"不一致={wrong_stage}" if wrong_stage else "全部一致")
ok("G10c 门禁是**拦截式**的（写 .failed + exit 1），不是只打印警告",
   not weak_gate,
   f"只打印不拦截的阶段={weak_gate}" if weak_gate else
   "均已写成「未过 → 写 .failed → exit 1」")

print("\nG10c-2 成功路径要清掉陈旧的 .failed（否则重跑通过后被误报为未通过）", flush=True)
stale = []
for skill, stage in STAGE_SKILLS.items():
    p = os.path.join(SKILLS, skill, "SKILL.md")
    if not os.path.isfile(p):
        continue
    t = read(p)
    if f"rm -f \"${{RUN_DIR}}/.failed/{stage}.failed\"" not in t:
        stale.append(skill)
ok("G10c-2 每个阶段在写 .done 后都清掉同阶段的陈旧 .failed",
   not stale,
   f"缺清理的阶段={stale}" if stale else
   "全部已清 —— 陈旧状态与真实状态只留一个")

print("\nG10d 吞错的收尾写法不再出现（这是「错误传到后面」的直接成因）", flush=True)
swallow = []
for skill in STAGE_SKILLS:
    p = os.path.join(SKILLS, skill, "SKILL.md")
    if not os.path.isfile(p):
        continue
    for i, line in enumerate(read(p).splitlines(), 1):
        s = line.strip()
        if s.startswith("#") or s.startswith(">") or s.startswith("|"):
            continue
        # 说明性/劝阻性文本不算（文档里本就要举例说明这种写法是错的）
        if any(tok in line for tok in ("不要", "禁止", "⚠️", "会吞", "错误成因")):
            continue
        # `wait ... || true`（无累计）与 `wait ... || log_error ...`（无累计）
        if re.search(r"\bwait\b.*\|\|\s*true\b", s):
            swallow.append(f"{skill}:{i}: {s[:60]}")
        elif re.search(r"\bwait\b.*\|\|\s*log_error", s) and "FAILED" not in line:
            swallow.append(f"{skill}:{i}: {s[:60]}")
ok("G10d [缺陷确认] 阶段 skill 里没有「吞掉 wait 退出码」的收尾写法",
   not swallow,
   f"残留 {len(swallow)} 处：{swallow[:3]}" if swallow else
   "已全部改为累计 FAILED 并交收尾门禁裁决")

print("\nG10d-2 用到 FAILED 的模板必须先初始化它（set -u 下否则直接崩）", flush=True)
uninit = []
for skill in STAGE_SKILLS:
    p = os.path.join(SKILLS, skill, "SKILL.md")
    if not os.path.isfile(p):
        continue
    t = read(p)
    # 只在**可执行代码行**上判（说明性文本里提到 FAILED 不算）
    uses = [l for l in _code_lines(t)
            if re.search(r"\bFAILED\s*=\s*\$\(\(FAILED", l)]
    if uses and "FAILED=0" not in t:
        uninit.append(f"{skill}(用了 {len(uses)} 处却未定义)")
ok("G10d-2 [缺陷确认] 用到 FAILED 累计的模板都先定义了 FAILED=0",
   not uninit,
   f"未初始化={uninit}" if uninit else
   "全部已初始化 —— set -u 下 `FAILED=$((FAILED+1))` 遇未定义变量会直接崩")

print("\nG10e 编排层认得 .failed 状态（推进与恢复都要判它）", flush=True)
orch = read(os.path.join(PIPE, "SKILL.md"))
wfd = read(os.path.join(PIPE, "references", "workflow.md"))
ok("G10e-1 编排层 SKILL.md 定义了 .failed 语义",
   ".failed" in orch and "校验未通过" in orch)
ok("G10e-2 自动推进机制把 .failed 作为停止条件（不再只看 .done）",
   # 判据要容忍措辞与 markdown 标记：文档实际写的是
   #   2. 每次检查 `.done` 与 `.failed` 文件（**两者都要看**）
   #   5. `.failed` 存在 → **停止推进**，读失败原因如实报给用户
   # 早期版本写死带反引号的字面量 `.done` 存在，把措辞不同的**正确**文档误判为缺陷。
   bool(re.search(r"两者都要看|\.done.{0,8}与.{0,8}\.failed", orch))
   and bool(re.search(r"\.failed`?\s*存在\s*→\s*\**\s*停止推进", orch)),
   "自动推进段落应同时判 .done 与 .failed，且把 .failed 作为停止推进的条件")
ok("G10e-3 场景 B 恢复检查里 .failed 判定**优先于** .done/.pid",
   # .failed 的判断必须出现在 .done 判断之前 —— 顺序错了会把"校验未通过"
   # 误报成"已完成"或"已中断"，误导用户
   (lambda t: (t.find("${stage_name}.failed") != -1
               and t.find("${stage_name}.failed") < t.find("${stage_name}.done")))(orch),
   "四态判定顺序：.failed → .done → 运行中 → 未开始")
ok("G10e-4 workflow.md 的恢复检查同样处理 .failed",
   "failed_file" in wfd or ".failed" in wfd)
ok("G10e-5 输出目录结构里登记了 .failed/ 与 .verify/",
   ".failed/" in orch and ".verify/" in orch)

print("\nG10f verifier_path 进了 run.params.json 契约", flush=True)
pp = read(os.path.join(SKILLS, "wgs-express-params-propose", "SKILL.md"))
ok("G10f-1 params-propose 的 run.params.json 示例含 verifier_path",
   "verifier_path" in pp)
ok("G10f-2 说明了为何必须是绝对路径（阶段脚本与仓库无相对关系）",
   "绝对路径" in pp and "run.params.json" in pp)

print("\nG10g 表型侧门禁真的会阻止产出（判据有区分度）", flush=True)
ok("G10g-1 validate.py 存在且非空",
   os.path.isfile(PHENO_VALIDATE) and os.path.getsize(PHENO_VALIDATE) > 2000)
pt = read(os.path.join(SKILLS, "phenotype-extract", "scripts",
                       "phenotype_extract.py"))
ok("G10g-2 主脚本在写 GWAS 文件前调用门禁，且拦截时 sys.exit(1)",
   "validate_batch" in pt and "report.blocked" in pt and "sys.exit(1)" in pt)
ok("G10g-3 明细 CSV **无条件**先落盘（拦截时仍有据可查）",
   # CSV 落盘必须早于 report.blocked 判定 —— 否则拦截时连失败原因都没留下
   pt.find("to_csv(csv_path") < pt.find("report.blocked"),
   "明细 CSV 落盘 → 门禁判定 → 拦截时保留 CSV")
imf = read(os.path.join(SKILLS, "phenotype-extract", "scripts", "phenotypes",
                        "imf.py"))
_imf_code = "\n".join(_code_lines(imf))
ok("G10g-4 imf 抠图失败抛 PhenotypeError，不再用 max(meat_px, 1) 兜底",
   "PhenotypeError" in _imf_code and "max(meat_px, 1)" not in _imf_code,
   "兜底值会产出 IMF=0.0% —— 落在合法值域内、事后无法从数值上识别的假值")
ok("G10g-5 imf 声明了 value_range（门禁才有据可依）",
   "value_range" in imf)

# ── 端到端负控：门禁对坏数据必须真的返回非零 ──
print("\nG10g-端到端 用真实数据验证门禁会拦下坏产出", flush=True)
tmp = tempfile.mkdtemp(prefix="g10_")
try:
    import csv as _csv
    bad = os.path.join(tmp, "bad.csv")
    with open(bad, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.writer(f)
        w.writerow(["Taxa", "IMF_预测_%", "状态"])
        for i in range(20):
            w.writerow([f"s{i}", 0.0, "ok"])       # 全 0：抠图失败的典型伪装
    r = subprocess.run([sys.executable, PHENO_VALIDATE, "--csv", bad,
                        "--col", "IMF_预测_%", "--range", "0", "100", "--json"],
                       capture_output=True, text=True)
    ok("G10g-端到端1 全 0 值（落在值域内）被拦下且退出码非 0",
       r.returncode == 1 and '"blocked": true' in r.stdout,
       f"rc={r.returncode}")
    rep = json.loads(r.stdout)
    ok("G10g-端到端2 拦截原因是 not_constant（模型没真跑）",
       "not_constant" in rep.get("hard_failures", []),
       f"hard_failures={rep.get('hard_failures')}")
except Exception as e:                                              # noqa: BLE001
    ok("G10g-端到端", False, f"执行异常: {e!r}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n---- summary ----")
for l in log:
    print(l)
failed = sum(1 for l in log if l.startswith("FAIL"))
print("FAILED:", failed)
sys.exit(1 if failed else 0)

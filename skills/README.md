# skills/ — TraitPilot 自带技能

本目录是 Agent **自带**的技能库（不再依赖 Claude Code 的 `.claude/skills`）。
每个子目录即一个技能：

```
skills/<name>/SKILL.md          # 必需：frontmatter(name/description/allowed-tools/disable-model-invocation)
skills/<name>/usage-guide.md    # 可选：错误预防清单，随 SKILL.md 一并注入
skills/<name>/references/…      # 可选：参考资料，模型可继续 Read
skills/<name>/scripts/…         # 可选：脚本
```

发现来源只有两处：显式 `--skills-dir` → `<project>/skills`。
**不再回退 `.claude/skills`**（项目级或用户级）——只读本仓库自带的技能，避免混入外部技能。

## 本目录内容

18 个技能，`gwas-*` / `wgs-express-*` / `phenotype-extract`，均为 WGS / GWAS 分析管线。

## 模型权重（未随仓库分发）

`phenotype-extract/model/best_model.pth` 体积约 263MB，**已被 `.gitignore` 排除**，克隆后不存在。
需要跑 `phenotype-extract` 时，请把权重放到：

```
skills/phenotype-extract/model/best_model.pth
```

（与 `SKILL.md` / `usage-guide.md` 中引用的路径一致；来源见该技能的说明。）

---
name: skill-lab
description: 当 Codex 需要把本地 skill 安装进 Codex、运行可重复的自测或回归 suite、用独立 judge agent 审核 skill 效果、在失败后优化 SKILL.md，或用 scripts/codex_skill_tester.py 执行“安装 -> 测试 -> 判定 -> 修改 -> 复测”闭环时使用。用户要求验证或改进 Codex skill、调优工作流 prompt，或针对本地 skill 运行 suite/judge/autoloop 时也使用。
---

# Skill Lab（技能实验室）

这个 skill 使用随附的 Codex 测试工具：

`scripts/codex_skill_tester.py`

用于可重复的 Codex skill 评估、judge agent 语义判定、优化，以及自动自我改进闭环。

工作流明确参考 Andrej Karpathy 的 `karpathy/autoresearch` 项目：先建立 baseline，再让 agent 做实验式修改，基于证据判断是否改进，改进才保留，否则丢弃并回滚。

## 核心流程

1. 先把目标 skill 安装到 Codex。如果源码 skill 已经是 `$CODEX_HOME/skills` 下的当前安装版本，测试工具会自动 no-op。
2. 用 `suite` 收集可重复证据，用 `ask` 做单次 prompt 验证。
3. 面对复杂 skill 时，默认在 `suite` 后运行 `judge`，让独立判定 agent 做语义审核。
4. 用户需要完整的“安装 -> 自测 -> 判定 -> 修改 -> 复测”闭环时，先用 `suite -> judge -> optimize` 手动闭环；`autoloop` 只适合断言明确的窄回归。
5. 只有在失败的 suite、低质量 judge 结论或 autoloop 轮次暴露出真实弱点后，才使用 `optimize --apply`。
6. 只有 suite 指标和 judge 结论同时变好时才保留修改。

## 优先使用的命令

- `export SKILL_LAB_HOME="${CODEX_HOME:-$HOME/.codex}/skills/skill-lab"`
- `rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" install --skill <skill-path>`
- `rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" suite --skill <skill-path> --file <suite.json>`
- `rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" judge --suite-run-dir <suite-run-dir> --skill <skill-path>`
- `rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" ask --skill <skill-path> --prompt "<prompt>"`
- `rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" optimize --skill <skill-path> --apply`
- `rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" autoloop --skill <skill-path> --file <suite.json> --reasoning-effort medium --timeout 180 --max-rounds 3`

## 方法

- 把第一次 suite 运行当作 baseline。
- 评估前先把当前 skill 副本安装到 `$CODEX_HOME/skills/<skill-name>`，除非明确需要 `--no-install`。
- 把脚本断言当作证据收集，不把它当作最终裁判。
- 用 `judge` 审核 transcript、prompt、断言结果和当前 `SKILL.md`，输出 `PASS`、`WARN` 或 `FAIL`。
- 把每次优化都当作一次实验。
- 只有 suite 指标和 judge 结论同时变好时才保留修改。
- 指标或 judge 结论没有变好时，丢弃修改并回滚。
- 如果 Codex 没有产出 final message，先重试一次，再把该轮归类为 crash。

产物会写到当前仓库的 `.codex-tests/runs/` 下。

judge agent 的角色定义、判定口径和输出结构见 `references/judge-agent.md`。
命令参数和工作流细节见 `references/codex-skill-tester.md`。
随附 suite 模板见 `references/examples/template-suite.json` 和 `references/examples/template-failing-suite.json`。

## 约束

- shell 命令必须加 `rtk` 前缀。
- 评估前先把 skill 安装到 Codex，除非用户明确要用 `--no-install` 测试已有安装版本。
- 复杂 skill 的最终效果判定必须参考 `judge` 结果，不要只看字符串断言是否通过。
- 只有在已经准备好专用临时 `CODEX_HOME` 时才优先使用它；否则使用当前活跃的 Codex home，让 suite 保持贴近真实使用。
- 不要为了满足与真实任务无关的任意哨兵断言而优化 skill。

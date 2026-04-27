# Codex Skill Lab

Codex Skill Lab 是一个用于本地 Codex skill 评估、语义判定和迭代优化的 skill。

它的核心目标是避免只用字符串断言评判复杂 skill。脚本负责运行可重复测试并保存证据，judge agent 负责基于证据做语义审核，优化步骤只在真实弱点明确后执行。

## Related Work

[karpathy/autoresearch](https://github.com/karpathy/autoresearch) 展示了一个很有启发性的自动实验循环：让 agent 在真实环境中自主修改、运行、度量、比较结果；如果结果变好就保留，否则丢弃并继续下一轮。

Skill Lab 的工作流也采用类似的实验闭环：

- `suite` 对应可重复实验运行，负责收集证据。
- `judge` 对应实验结果评审，负责判断质量是否真的变好。
- `optimize` 对应下一轮实验提案，只基于失败或低质量证据做最小修改。
- `keep/discard` 原则用于避免为了单个机械断言牺牲整体质量。

## 核心原理

复杂 skill 的质量问题通常不只是“有没有包含某个字符串”。更重要的是：

- 是否在正确场景触发 skill
- 是否遵守 `SKILL.md` 的关键流程
- 是否产出真实可用的交付物
- 是否在失败、模糊、多步骤场景中保持稳定
- 是否有必要的验证意识
- 是否避免为了通过测试而迎合无意义断言

Skill Lab 把审核拆成三层：

1. `suite`：运行一组可重复 case，收集 prompt、最终回复、断言结果和运行元数据。
2. `judge`：启动独立 Codex judge agent，读取 suite 证据和当前 `SKILL.md`，输出 `PASS`、`WARN` 或 `FAIL`。
3. `optimize`：只在 suite 失败或 judge 认为质量不足时，基于失败证据提出最小 `SKILL.md` 修改。

推荐闭环是：

```text
install -> suite -> judge -> optimize -> suite -> judge
```

`autoloop` 仍可用于窄回归和烟测，但复杂 skill 的发布级审核应优先使用手动的 `suite -> judge` 判定链路。

## 目录结构

```text
.
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── codex-skill-tester.md
│   ├── judge-agent.md
│   ├── openclaw-skill-tester.md
│   └── examples/
│       ├── template-suite.json
│       └── template-failing-suite.json
└── scripts/
    ├── codex_skill_tester.py
    └── openclaw_skill_tester.py
```

## 快速开始

安装或同步目标 skill：

```bash
export SKILL_LAB_HOME="${CODEX_HOME:-$HOME/.codex}/skills/skill-lab"
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" install --skill path/to/your-skill
```

运行 suite：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" suite \
  --skill path/to/your-skill \
  --file path/to/suite.json
```

运行 judge agent：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" judge \
  --suite-run-dir .codex-tests/runs/<timestamp>-suite-<skill> \
  --skill path/to/your-skill
```

如果当前 Codex CLI 的默认模型不可用，可以显式指定模型：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" --model gpt-5.2 suite \
  --skill path/to/your-skill \
  --file path/to/suite.json
```

## Judge Agent

Judge agent 的定义在 `references/judge-agent.md`。它会审核：

- 任务完成度
- skill 遵循度
- 交付质量
- 复杂场景能力
- 验证意识
- 风险控制

输出是结构化 JSON，核心字段包括：

- `verdict`: `PASS`、`WARN` 或 `FAIL`
- `score`: 0-100
- `should_optimize`
- `case_reviews`
- `suite_issues`
- `improvement_recommendations`
- `regression_risks`

## 运行产物

测试产物默认写入：

```text
.codex-tests/runs/
```

这些文件可能包含 prompt、模型回复、路径和运行元数据，默认不会提交到仓库。

## 安全说明

本仓库不应提交：

- `.codex-tests/` 运行产物
- 本地临时 skill
- token、cookie、API key、SSH key 或个人配置
- 包含真实用户数据的 transcript

提交前建议运行敏感信息扫描，并人工检查新增文件。

# Codex Skill Tester（Codex Skill 测试器）

这套流程使用实验式迭代方法：`suite` 收集实验证据，`judge` 做语义判定，`optimize` 只在证据显示真实弱点时提出修改。复杂 skill 的最终判断交给独立 judge agent：

- 第一轮永远先跑 baseline
- 后续每轮只围绕一个目标 skill 做修改
- 每轮都记录结果
- `suite` 负责收集可重复证据
- `judge` 负责基于证据做语义判定
- suite 指标和 judge 结论都改进才 `keep`
- 没改进就 `discard` 并回滚到当前最佳版本
- 遇到明显瞬态故障时自动重试一次

这个工具让你在当前仓库里直接做一条完整闭环：

1. 把某个 skill 安装到当前 Codex home 的 `skills/`
2. 用 `codex exec` 跑单轮或多轮测试
3. 保存最终回复、事件流、session id、suite 结果
4. 用 JSON suite 做回归测试
5. 用 judge agent 审核 suite 运行证据
6. 从失败或低质量 suite 自动回溯并生成 `SKILL.md` 优化建议
7. 自动执行“安装 -> 自测 -> 判定 -> 修改 -> 复测”闭环

工具脚本：

```bash
export SKILL_LAB_HOME="${CODEX_HOME:-$HOME/.codex}/skills/skill-lab"
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" --help
```

如果子进程报错类似 `The 'gpt-5.5' model requires a newer version of Codex`，说明当前 Codex CLI 的默认模型配置和 CLI 版本不匹配。直接给命令加全局参数 `--model <当前 CLI 支持的模型>`，例如本地 smoke test 可用：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" --model gpt-5.2 suite \
  --skill path/to/your-skill \
  --file path/to/suite.json
```

## 1. 安装 skill 到 Codex

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" install \
  --skill path/to/your-skill
```

默认会把 skill 安装到当前 `CODEX_HOME` 对应的 `skills/` 目录；如果没有设置 `CODEX_HOME`，就是 `~/.codex/skills/`。

如果源码 skill 已经就在这个目录下，安装步骤会自动 no-op，不会删除自己再复制自己。

## 2. 单轮测试

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" ask \
  --skill path/to/your-skill \
  --prompt "用一个真实用户请求来验证这个 skill 会不会直接产出目标交付物。" \
  --print-meta
```

运行后会把产物保存到：

```text
.codex-tests/runs/<timestamp>-<skill>-ask/
```

关键文件：

- `request.json`
- `response.json`
- `summary.json`
- `transcript.md`

## 3. 多轮对话测试

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" repl \
  --skill path/to/your-skill
```

CLI 会在第一轮创建一个可恢复的 Codex session，后续轮次复用该 `session_id`。适合边聊边观察 skill 是否真的遵循了预期结构。

## 4. 回归 suite

示例：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" suite \
  --skill path/to/your-skill \
  --file "$SKILL_LAB_HOME/references/examples/template-suite.json"
```

这些模板文件需要你先把其中的 `skill`、`prompt` 和断言改成自己的目标 skill。

Suite 文件格式是 JSON：

```json
{
  "skill": "path/to/your-skill",
  "cases": [
    {
      "name": "基础交付物检查",
      "prompt": "针对一个真实用户请求，产出这个 skill 应该创建的主要交付物。",
      "assert_contains": ["替换为期望标题"],
      "assert_not_contains": ["SKILL_NOT_FOUND"],
      "min_reply_chars": 200
    }
  ]
}
```

如果任何 case 断言失败，命令会返回退出码 `2`，方便后续接进自动化流程。

## 5. 语义判定 agent

复杂 skill 不建议只靠 `assert_contains` 这类机械断言下结论。先跑 suite 收集证据，再让独立 judge agent 审核：

judge agent 的完整角色定义、判定口径和 JSON 输出结构在：

- `references/judge-agent.md`

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" judge \
  --suite-run-dir .codex-tests/runs/<timestamp>-suite-<skill> \
  --skill path/to/your-skill
```

如果不传 `--suite-run-dir`，工具会自动选择最近一次 suite run。judge agent 会读取：

- 当前 `SKILL.md`
- suite summary
- 每个 case 的 prompt
- 每个 case 的最终回复
- 机械断言结果

默认会读取 `references/judge-agent.md` 作为 judge agent 定义。它会审核：

- 是否真正完成用户任务
- 是否遵守目标 skill 的关键工作流
- 交付物是否具体、准确、可直接使用
- 复杂或模糊场景是否处理稳健
- 是否有必要的验证意识
- 是否避免无关修改、虚构能力和机械迎合断言

输出目录会包含：

- `judge.json`
- `request.json`
- `response.json`
- `summary.json`
- 解析失败时的 `raw-reply.txt`

`judge.json` 的核心字段：

```json
{
  "verdict": "PASS | WARN | FAIL",
  "score": 0,
  "summary": "整体判断",
  "should_optimize": true,
  "case_reviews": [],
  "suite_issues": [],
  "improvement_recommendations": [],
  "regression_risks": []
}
```

你也可以传自定义 rubric：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" judge \
  --suite-run-dir .codex-tests/runs/<timestamp>-suite-<skill> \
  --skill path/to/your-skill \
  --rubric-file path/to/rubric.md
```

推荐判定策略：

- `PASS`：可以保留当前版本，但仍可记录轻微改进项
- `WARN`：不要急着发布，先看 `improvement_recommendations`
- `FAIL`：先修 skill 或 suite，再复测
- 如果机械断言通过但 judge 给 `WARN` 或 `FAIL`，以 judge 结论为准
- 如果机械断言失败但 judge 认为任务完成，要修 suite 断言，而不是硬改 skill

## 6. 根据失败或低质量 suite 生成 skill 优化建议

先跑出一个失败或 judge 判定质量不足的 suite，然后执行：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" suite \
  --skill path/to/your-skill \
  --file "$SKILL_LAB_HOME/references/examples/template-failing-suite.json"

rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" optimize \
  --skill path/to/your-skill
```

如果不传 `--suite-run-dir`，工具会自动寻找最近一次失败的 suite。

输出目录里会包含：

- `optimization.json`
- `proposed-SKILL.md`
- `skill.diff`
- `response.json`

如果你确认建议合适，也可以直接覆盖源码里的 `SKILL.md`：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" optimize \
  --skill path/to/your-skill \
  --apply
```

默认仍建议先看 `skill.diff` 再决定是否应用。

## 7. 自动闭环

如果你想直接让工具执行多轮：

```bash
rtk python3 "$SKILL_LAB_HOME/scripts/codex_skill_tester.py" autoloop \
  --skill path/to/your-skill \
  --file "$SKILL_LAB_HOME/references/examples/template-failing-suite.json" \
  --reasoning-effort medium \
  --timeout 180 \
  --max-rounds 3
```

默认逻辑是：

1. 跑一次 suite
2. 这轮结果记入 `results.tsv` 作为 baseline 或候选实验
3. 如果失败，自动执行 `optimize --apply`
4. 再跑 suite
5. 如果结果比当前最佳更好，就 `keep`
6. 如果结果没有更好，就 `discard` 并回滚到当前最佳 `SKILL.md`
7. 直到通过，或者耗尽 `--max-rounds`

注意：`autoloop` 当前主要看 suite 断言指标，适合窄回归和烟测。复杂 skill 的发布级审核，应优先使用手动的 `suite -> judge -> optimize -> suite -> judge` 闭环。

运行结束后会在 `.codex-tests/runs/...-autoloop-*` 下写出：

- `autoloop-summary.json`
- `results.tsv`
- `original-SKILL.md`
- `best-SKILL.md`
- 每轮 `suite` 的 stdout/stderr
- 每轮 `optimize` 的 stdout/stderr

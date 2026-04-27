#!/usr/bin/env python3
"""Install local skills into Codex and run repeatable skill tests."""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
DEFAULT_SKILLS_DIR = DEFAULT_CODEX_HOME / "skills"
DEFAULT_RUNS_DIR = Path(".codex-tests") / "runs"
DEFAULT_JUDGE_AGENT_REFERENCE = Path(__file__).resolve().parents[1] / "references" / "judge-agent.md"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_SANDBOX = "workspace-write"
DEFAULT_APPROVAL = "never"
EXIT_ASSERTION_FAILED = 2
OPTIMIZATION_SCHEMA = {
    "summary": "string",
    "root_causes": ["string"],
    "recommended_edits": [
        {
            "section": "string",
            "problem": "string",
            "change": "string",
        }
    ],
    "suggested_test_updates": ["string"],
    "updated_skill_markdown_b64": "base64-encoded UTF-8 string",
}
OPTIMIZATION_FORMAT = """SUMMARY:
<one-paragraph summary>

ROOT_CAUSES:
- cause 1
- cause 2

RECOMMENDED_EDITS:
- section :: problem :: change

SUGGESTED_TEST_UPDATES:
- update 1

UPDATED_SKILL_MD_BEGIN
<full revised SKILL.md markdown>
UPDATED_SKILL_MD_END"""
MINIMAL_OPTIMIZATION_FORMAT = """SUMMARY: <one-line summary>

UPDATED_SKILL_MD_BEGIN
<full revised SKILL.md markdown>
UPDATED_SKILL_MD_END"""
JUDGE_SCHEMA = {
    "verdict": "PASS | WARN | FAIL",
    "score": "integer 0-100",
    "summary": "string",
    "should_optimize": "boolean",
    "case_reviews": [
        {
            "name": "string",
            "verdict": "PASS | WARN | FAIL",
            "score": "integer 0-100",
            "strengths": ["string"],
            "issues": ["string"],
            "evidence": ["string"],
        }
    ],
    "suite_issues": ["string"],
    "improvement_recommendations": ["string"],
    "regression_risks": ["string"],
}
DEFAULT_JUDGE_RUBRIC = """审核目标：
- 判断目标 skill 在真实用户任务中是否稳定地产出正确、完整、可用的交付物。
- 重点看语义质量、流程遵循、边界处理、工具使用判断和验证意识，不只看字符串断言。

评分维度：
- 任务完成度：是否真正解决用户请求，而不是只解释流程。
- skill 遵循度：是否触发并遵守目标 SKILL.md 的关键约束和工作流。
- 交付质量：结果是否具体、准确、结构合理、可直接使用。
- 复杂场景能力：面对模糊、失败、跨步骤或多产物任务时是否处理稳健。
- 验证意识：是否在需要时检查产物、命令结果或可观测证据。
- 风险控制：是否避免无关修改、虚构能力、过度承诺或机械迎合断言。

判定标准：
- PASS：主要任务都完成，只有轻微可改进项。
- WARN：可用但存在明显质量、稳定性或覆盖缺口。
- FAIL：没有完成核心任务、违背 skill 关键约束，或输出不可可靠使用。
"""


class TesterError(RuntimeError):
    """Raised when the CLI cannot complete the requested test workflow."""


@dataclass(frozen=True)
class SkillSpec:
    source_dir: Path
    install_name: str
    skill_name: str

    @property
    def skill_file(self) -> Path:
        return self.source_dir / "SKILL.md"


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    slug = slug.strip("-")
    return slug or "run"


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def resolve_codex_home(configured_path: str | None) -> Path:
    if configured_path:
        return expand_path(configured_path)
    return DEFAULT_CODEX_HOME.resolve()


def resolve_skills_dir(configured_path: str | None, codex_home: Path) -> Path:
    if configured_path:
        return expand_path(configured_path)
    return (codex_home / "skills").resolve()


def load_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_default_judge_definition() -> str:
    if DEFAULT_JUDGE_AGENT_REFERENCE.exists():
        return DEFAULT_JUDGE_AGENT_REFERENCE.read_text(encoding="utf-8")
    return DEFAULT_JUDGE_RUBRIC


def read_skill_name(skill_file: Path) -> str | None:
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None

    match = re.search(r"^name:\s*([^\n]+)$", text, flags=re.MULTILINE)
    if not match:
        return None
    raw_name = match.group(1).strip()
    return raw_name.strip("\"'")


def candidate_skill_dirs(search_roots: list[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for root in search_roots:
        if not root.exists():
            continue
        if root.is_file() and root.name == "SKILL.md":
            resolved = root.parent.resolve()
            found[str(resolved)] = resolved
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("SKILL.md"):
            resolved = path.parent.resolve()
            found[str(resolved)] = resolved
    return sorted(found.values(), key=lambda path: str(path))


def resolve_skill_spec(skill_value: str, repo_root: Path, install_name: str | None) -> SkillSpec:
    repo_root = repo_root.resolve()
    direct_path = Path(skill_value).expanduser()
    default_skills_dir = DEFAULT_SKILLS_DIR.resolve()
    search_paths = []

    if direct_path.is_absolute():
        search_paths.append(direct_path)
    else:
        search_paths.append((repo_root / direct_path).resolve())
        search_paths.append((Path.cwd() / direct_path).resolve())
        search_paths.append((default_skills_dir / direct_path).resolve())

    for path in search_paths:
        if path.is_file() and path.name == "SKILL.md":
            resolved_dir = path.parent.resolve()
            frontmatter_name = read_skill_name(resolved_dir / "SKILL.md") or resolved_dir.name
            return SkillSpec(
                source_dir=resolved_dir,
                install_name=install_name or resolved_dir.name,
                skill_name=frontmatter_name,
            )
        if path.is_dir() and (path / "SKILL.md").exists():
            resolved_dir = path.resolve()
            frontmatter_name = read_skill_name(resolved_dir / "SKILL.md") or resolved_dir.name
            return SkillSpec(
                source_dir=resolved_dir,
                install_name=install_name or resolved_dir.name,
                skill_name=frontmatter_name,
            )

    matches: list[Path] = []
    for candidate in candidate_skill_dirs([repo_root, Path.cwd().resolve(), default_skills_dir]):
        if candidate.name == skill_value:
            matches.append(candidate)
            continue
        frontmatter_name = read_skill_name(candidate / "SKILL.md")
        if frontmatter_name == skill_value:
            matches.append(candidate)

    deduped = sorted({str(path.resolve()): path.resolve() for path in matches}.values(), key=lambda path: str(path))
    if not deduped:
        raise TesterError(f"Could not find skill '{skill_value}' under {repo_root} or {default_skills_dir}")
    if len(deduped) > 1:
        rendered = "\n".join(f"- {path}" for path in deduped)
        raise TesterError(
            f"Skill '{skill_value}' matched multiple directories. Use an explicit path.\n{rendered}"
        )

    resolved_dir = deduped[0]
    frontmatter_name = read_skill_name(resolved_dir / "SKILL.md") or resolved_dir.name
    return SkillSpec(
        source_dir=resolved_dir,
        install_name=install_name or resolved_dir.name,
        skill_name=frontmatter_name,
    )


def remove_path(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


def install_skill(skill: SkillSpec, skills_dir: Path, prune: bool = True) -> Path:
    skills_dir.mkdir(parents=True, exist_ok=True)
    destination = skills_dir / skill.install_name

    if destination.exists() or destination.is_symlink():
        try:
            if destination.resolve() == skill.source_dir.resolve():
                return destination
        except OSError:
            pass
        if prune:
            remove_path(destination)

    shutil.copytree(
        skill.source_dir,
        destination,
        dirs_exist_ok=not prune,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    return destination


def command_prefix(use_rtk: bool) -> list[str]:
    if use_rtk and shutil.which("rtk"):
        return ["rtk"]
    return []


def run_command(
    command: list[str],
    cwd: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TesterError(
            "Command timed out.\n"
            f"Command: {' '.join(command)}\n"
            f"Timeout: {timeout_seconds}s\n"
            f"STDOUT:\n{exc.stdout or ''}\n"
            f"STDERR:\n{exc.stderr or ''}"
        ) from exc


def extract_json_blob(raw_text: str) -> Any:
    stripped = raw_text.strip()
    if not stripped:
        raise TesterError("Codex returned no output.")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise TesterError("Could not find JSON in model output.")
    return json.loads(stripped[start : end + 1])


def parse_jsonl_events(raw_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            events.append(payload)
    return events


def normalize_reasoning_effort(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"off", "none"}:
        return "minimal"
    return normalized


def build_codex_command_args(
    *,
    message: str,
    output_last_message: Path,
    session_id: str | None,
    model: str | None,
    reasoning_effort: str | None,
    sandbox: str,
    approval: str,
    add_dirs: list[Path],
    use_rtk: bool,
    ephemeral: bool,
    dangerously_bypass_approvals_and_sandbox: bool,
) -> list[str]:
    args = [*command_prefix(use_rtk), "codex"]
    if dangerously_bypass_approvals_and_sandbox:
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        args.extend(["-a", approval, "-s", sandbox])
    for add_dir in add_dirs:
        args.extend(["--add-dir", str(add_dir)])

    if session_id:
        args.extend(
            [
                "exec",
                "resume",
                "--json",
                "--color",
                "never",
                "--skip-git-repo-check",
                "-o",
                str(output_last_message),
            ]
        )
    else:
        args.extend(
            [
                "exec",
                "--json",
                "--color",
                "never",
                "--skip-git-repo-check",
                "-o",
                str(output_last_message),
            ]
        )
        if ephemeral:
            args.append("--ephemeral")

    if model:
        args.extend(["-m", model])
    if reasoning_effort:
        args.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])

    if session_id:
        args.extend([session_id, message])
    else:
        args.append(message)
    return args


def extract_reply_text_from_events(events: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return "\n\n".join(texts).strip()


def extract_meta(
    events: list[dict[str, Any]],
    *,
    model: str | None,
    reasoning_effort: str | None,
    exit_code: int,
) -> dict[str, Any]:
    thread_id: str | None = None
    usage: dict[str, Any] = {}
    for event in events:
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = str(event["thread_id"])
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]

    return {
        "session_id": thread_id,
        "thread_id": thread_id,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "usage": usage,
        "event_count": len(events),
        "exit_code": exit_code,
    }


def run_codex_turn(
    *,
    repo_root: Path,
    codex_home: Path,
    message: str,
    session_id: str | None,
    model: str | None,
    reasoning_effort: str | None,
    sandbox: str,
    approval: str,
    add_dirs: list[Path],
    timeout: int,
    use_rtk: bool,
    ephemeral: bool,
    dangerously_bypass_approvals_and_sandbox: bool,
) -> dict[str, Any]:
    output_handle = tempfile.NamedTemporaryFile(prefix="codex-skill-lab-", suffix=".txt", delete=False)
    output_handle.close()
    output_last_message = Path(output_handle.name)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)

    command = build_codex_command_args(
        message=message,
        output_last_message=output_last_message,
        session_id=session_id,
        model=model,
        reasoning_effort=normalize_reasoning_effort(reasoning_effort),
        sandbox=sandbox,
        approval=approval,
        add_dirs=add_dirs,
        use_rtk=use_rtk,
        ephemeral=ephemeral,
        dangerously_bypass_approvals_and_sandbox=dangerously_bypass_approvals_and_sandbox,
    )
    proc = run_command(command, cwd=repo_root, env=env, timeout_seconds=timeout)
    events = parse_jsonl_events(proc.stdout)
    reply_text = ""
    if output_last_message.exists():
        reply_text = output_last_message.read_text(encoding="utf-8").strip()
        output_last_message.unlink(missing_ok=True)
    if not reply_text:
        reply_text = extract_reply_text_from_events(events)

    if proc.returncode != 0:
        raise TesterError(
            "Codex command failed.\n"
            f"Command: {' '.join(command)}\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )
    if not reply_text:
        raise TesterError("Codex produced no final message.")

    meta = extract_meta(
        events,
        model=model,
        reasoning_effort=normalize_reasoning_effort(reasoning_effort),
        exit_code=proc.returncode,
    )
    return {
        "events": events,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
        "reply_text": reply_text,
        "meta": meta,
    }


def describe_skill_installation(skill: SkillSpec, skills_dir: Path) -> str:
    skill_file = skills_dir / skill.install_name / "SKILL.md"
    if skill_file.exists():
        return (
            "Skill installed into Codex.\n\n"
            f"Details:\n"
            f"  Skill name: {skill.skill_name}\n"
            f"  Install dir: {skill_file.parent}\n"
            f"  Skills dir: {skills_dir}"
        )
    raise TesterError(f"Expected installed skill file missing: {skill_file}")


def build_skill_test_message(skill_name: str, prompt: str, install_path: Path | None = None) -> str:
    location_line = f"Installed path: {install_path}" if install_path else "Installed path: unknown"
    return (
        "You are running a local Codex skill regression test.\n"
        f"Target skill: {skill_name}\n"
        f"{location_line}\n"
        "Instructions:\n"
        "- Treat this turn as a standalone evaluation.\n"
        "- Ignore previous conversation context, prior outputs, and remembered user preferences from this session.\n"
        "- Do not say you already completed the task earlier.\n"
        "- Do not summarize prior turns before answering.\n"
        "- Produce the requested deliverable directly in this turn.\n"
        f"- If the target skill is available and relevant, use `${skill_name}` as the primary workflow.\n"
        "- Stay close to the skill's structure, constraints, and output format.\n"
        f"- If the skill is unavailable, begin your answer with `SKILL_NOT_FOUND: {skill_name}`.\n"
        "- Keep the answer focused on the user's task.\n\n"
        "User task:\n"
        f"{prompt.strip()}\n"
    )


def create_run_dir(runs_dir: Path, label: str) -> Path:
    run_dir = runs_dir / f"{timestamp_slug()}-{slugify(label)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def render_diff_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path.resolve())


def write_single_run_artifacts(
    run_dir: Path,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    reply_text: str,
    meta: dict[str, Any],
    skill_info_text: str | None,
) -> None:
    dump_json_file(run_dir / "request.json", request_payload)
    dump_json_file(run_dir / "response.json", response_payload)
    dump_json_file(run_dir / "summary.json", {"reply_text": reply_text, "meta": meta})

    transcript_lines = [
        "# Codex Skill Test",
        "",
        f"- Skill: `{request_payload.get('skill_name', '')}`",
        f"- Session ID: `{meta.get('session_id')}`",
        f"- Model: `{meta.get('model') or 'default'}`",
        f"- Reasoning Effort: `{meta.get('reasoning_effort') or 'default'}`",
        f"- Output Tokens: `{meta.get('usage', {}).get('output_tokens')}`",
        "",
        "## Prompt",
        "",
        request_payload.get("prompt", ""),
        "",
        "## Reply",
        "",
        reply_text,
        "",
    ]
    if skill_info_text:
        transcript_lines.extend(["## Skill Install", "", "```text", skill_info_text, "```", ""])
    (run_dir / "transcript.md").write_text("\n".join(transcript_lines), encoding="utf-8")


def standalone_regression_prefix() -> str:
    return (
        "Treat this turn as a standalone evaluation.\n"
        "Ignore previous conversation context, prior outputs, and remembered user preferences from this session.\n"
        "Do not say you already completed the task earlier.\n"
        "Do not summarize prior turns before answering.\n"
    )


def normalize_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt
    if args.prompt_file:
        return expand_path(args.prompt_file).read_text(encoding="utf-8")
    raise TesterError("A prompt is required. Use --prompt or --prompt-file.")


def evaluate_case_assertions(reply_text: str, case: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    for needle in case.get("assert_contains", []):
        if needle not in reply_text:
            failures.append(f"missing expected text: {needle}")

    for needle in case.get("assert_not_contains", []):
        if needle in reply_text:
            failures.append(f"unexpected text present: {needle}")

    for pattern in case.get("assert_regex", []):
        if re.search(pattern, reply_text, flags=re.MULTILINE) is None:
            failures.append(f"regex did not match: {pattern}")

    for pattern in case.get("assert_not_regex", []):
        if re.search(pattern, reply_text, flags=re.MULTILINE) is not None:
            failures.append(f"regex matched unexpectedly: {pattern}")

    min_chars = case.get("min_reply_chars")
    if min_chars is not None and len(reply_text) < int(min_chars):
        failures.append(f"reply shorter than min_reply_chars={min_chars}")

    return failures


def suite_run_directories(runs_dir: Path) -> list[Path]:
    if not runs_dir.exists():
        return []
    return sorted(
        [path for path in runs_dir.iterdir() if path.is_dir() and (path / "suite-summary.json").exists()],
        key=lambda path: path.name,
        reverse=True,
    )


def load_suite_summary(run_dir: Path) -> dict[str, Any]:
    payload = load_json_file(run_dir / "suite-summary.json")
    if not isinstance(payload, dict):
        raise TesterError(f"Suite summary is not a JSON object: {run_dir / 'suite-summary.json'}")
    return payload


def find_latest_suite_run(
    runs_dir: Path,
    *,
    only_failed: bool,
    skill_name: str | None = None,
) -> Path:
    for run_dir in suite_run_directories(runs_dir):
        summary = load_suite_summary(run_dir)
        if skill_name and summary.get("skill_name") != skill_name:
            continue
        failed_cases = int(summary.get("failed_cases", 0))
        if only_failed and failed_cases <= 0:
            continue
        return run_dir
    failure_label = "failed " if only_failed else ""
    skill_label = f" for skill '{skill_name}'" if skill_name else ""
    raise TesterError(f"Could not find a {failure_label}suite run under {runs_dir}{skill_label}.")


def truncate_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    return value[:max_chars].rstrip() + f"\n\n[truncated {omitted} chars]"


def load_case_records(
    run_dir: Path,
    *,
    failed_only: bool = False,
    max_reply_chars: int = 0,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for case_dir in sorted([path for path in run_dir.iterdir() if path.is_dir()]):
        result_path = case_dir / "result.json"
        case_path = case_dir / "case.json"
        if not result_path.exists() or not case_path.exists():
            continue
        result_payload = load_json_file(result_path)
        case_payload = load_json_file(case_path)
        if not isinstance(result_payload, dict) or not isinstance(case_payload, dict):
            continue
        passed = bool(result_payload.get("passed", False))
        if failed_only and passed:
            continue
        reply_text = str(result_payload.get("reply_text", ""))
        records.append(
            {
                "name": case_payload.get("name"),
                "prompt": case_payload.get("prompt"),
                "assertions": case_payload.get("assertions", {}),
                "reply_text": truncate_text(reply_text, max_reply_chars),
                "failures": result_payload.get("failures", []),
                "passed": passed,
                "meta": result_payload.get("meta", {}),
                "case_dir": str(case_dir),
            }
        )
    return records


def load_failed_case_records(run_dir: Path) -> list[dict[str, Any]]:
    return load_case_records(run_dir, failed_only=True)


def resolve_skill_spec_from_suite_run(
    run_dir: Path,
    repo_root: Path,
    install_name: str | None,
    explicit_skill: str | None = None,
) -> SkillSpec:
    if explicit_skill:
        return resolve_skill_spec(explicit_skill, repo_root=repo_root, install_name=install_name)

    summary = load_suite_summary(run_dir)
    suite_file = summary.get("suite_file")
    if isinstance(suite_file, str):
        suite_path = Path(suite_file).expanduser()
        if not suite_path.is_absolute():
            suite_path = (repo_root / suite_path).resolve()
        if suite_path.exists():
            suite_payload = load_json_file(suite_path)
            if isinstance(suite_payload, dict) and isinstance(suite_payload.get("skill"), str):
                return resolve_skill_spec(
                    suite_payload["skill"],
                    repo_root=repo_root,
                    install_name=install_name,
                )

    skill_name = summary.get("skill_name")
    if isinstance(skill_name, str):
        return resolve_skill_spec(skill_name, repo_root=repo_root, install_name=install_name)
    raise TesterError(f"Could not resolve a skill from suite run: {run_dir}")


def build_skill_optimization_message(
    skill: SkillSpec,
    skill_markdown: str,
    suite_summary: dict[str, Any],
    failed_cases: list[dict[str, Any]],
) -> str:
    payload = {
        "skill_name": skill.skill_name,
        "suite_summary": {
            "failed_cases": suite_summary.get("failed_cases"),
            "passed_cases": suite_summary.get("passed_cases"),
            "suite_file": suite_summary.get("suite_file"),
        },
        "failed_cases": failed_cases,
    }
    return (
        "You are improving a local Codex skill after a failed regression suite.\n"
        f"{standalone_regression_prefix()}"
        "Return the minimal plain-text marker format below.\n"
        "Do not wrap the response in markdown fences.\n"
        "Preserve the YAML front matter in the revised skill markdown.\n"
        "Only edit the skill instructions needed to improve future test outcomes.\n"
        "Do not mention git, approvals, or external setup steps in the revised skill.\n"
        "Fold all rationale into the single SUMMARY line.\n"
        f"Preferred marker format:\n{MINIMAL_OPTIMIZATION_FORMAT}\n\n"
        "If you cannot follow the minimal marker format, fall back to either:\n"
        f"1. valid JSON matching this schema: {json.dumps(OPTIMIZATION_SCHEMA, ensure_ascii=False)}\n"
        f"2. the detailed marker format: {OPTIMIZATION_FORMAT}\n\n"
        f"Target skill: {skill.skill_name}\n"
        f"Skill path: {skill.skill_file}\n\n"
        "Current SKILL.md:\n"
        "```md\n"
        f"{skill_markdown}\n"
        "```\n\n"
        "Failed suite context:\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```\n"
    )


def build_skill_raw_rewrite_message(
    skill: SkillSpec,
    skill_markdown: str,
    suite_summary: dict[str, Any],
    failed_cases: list[dict[str, Any]],
) -> str:
    payload = {
        "skill_name": skill.skill_name,
        "suite_summary": {
            "failed_cases": suite_summary.get("failed_cases"),
            "passed_cases": suite_summary.get("passed_cases"),
            "suite_file": suite_summary.get("suite_file"),
        },
        "failed_cases": failed_cases,
    }
    return (
        "You are rewriting a local Codex SKILL.md after a failed regression suite.\n"
        f"{standalone_regression_prefix()}"
        "Return only the full revised SKILL.md markdown.\n"
        "Do not add explanations, code fences, JSON, or extra notes.\n"
        "Preserve the YAML front matter.\n"
        "Make the skill more likely to produce the full requested deliverable directly in-chat.\n"
        "Do not optimize for arbitrary impossible sentinel strings unrelated to the user task.\n\n"
        f"Target skill: {skill.skill_name}\n"
        f"Skill path: {skill.skill_file}\n\n"
        "Current SKILL.md:\n"
        "```md\n"
        f"{skill_markdown}\n"
        "```\n\n"
        "Failed suite context:\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```\n"
    )


def build_judge_message(
    skill: SkillSpec,
    skill_markdown: str,
    suite_summary: dict[str, Any],
    case_records: list[dict[str, Any]],
    rubric_text: str,
) -> str:
    payload = {
        "skill_name": skill.skill_name,
        "suite_summary": {
            "failed_cases": suite_summary.get("failed_cases"),
            "passed_cases": suite_summary.get("passed_cases"),
            "suite_file": suite_summary.get("suite_file"),
            "case_results": suite_summary.get("case_results", []),
        },
        "case_records": case_records,
    }
    return (
        "你是一个独立的 Codex skill 效果判定 agent。\n"
        f"{standalone_regression_prefix()}"
        "你的任务是基于证据审核目标 skill 的真实效果，而不是机械复述 suite 断言结果。\n"
        "不要修改文件，不要提出需要用户手工补充才能完成的判定。\n"
        "如果机械断言通过但语义质量不足，你可以判为 WARN 或 FAIL。\n"
        "如果机械断言失败但回复实际满足用户任务，你可以解释该断言为何不合理。\n"
        "必须只返回一个合法 JSON 对象，不要使用 markdown 代码块。\n"
        f"JSON schema: {json.dumps(JUDGE_SCHEMA, ensure_ascii=False)}\n\n"
        f"目标 skill: {skill.skill_name}\n"
        f"Skill path: {skill.skill_file}\n\n"
        "Judge agent 定义和审核 rubric:\n"
        "```text\n"
        f"{rubric_text.strip()}\n"
        "```\n\n"
        "当前 SKILL.md:\n"
        "```md\n"
        f"{skill_markdown}\n"
        "```\n\n"
        "Suite 运行证据:\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```\n"
    )


def make_unified_diff(before: str, after: str, path_label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path_label}",
            tofile=f"b/{path_label}",
        )
    )


def validate_optimization_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TesterError("Optimization reply is not a JSON object.")
    required_keys = ("summary", "root_causes", "recommended_edits", "suggested_test_updates")
    for key in required_keys:
        if key not in payload:
            raise TesterError(f"Optimization reply is missing required key: {key}")
    if isinstance(payload.get("updated_skill_markdown"), str) and payload["updated_skill_markdown"].strip():
        return payload

    encoded = payload.get("updated_skill_markdown_b64")
    if not isinstance(encoded, str) or not encoded.strip():
        raise TesterError("Optimization reply is missing `updated_skill_markdown_b64` content.")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except Exception as exc:  # pragma: no cover - defensive parsing
        raise TesterError(f"Could not decode `updated_skill_markdown_b64`: {exc}") from exc
    if not decoded.strip():
        raise TesterError("Decoded `updated_skill_markdown_b64` is empty.")
    payload["updated_skill_markdown"] = decoded
    return payload


def validate_judge_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TesterError("Judge reply is not a JSON object.")
    for key in ("verdict", "score", "summary", "case_reviews"):
        if key not in payload:
            raise TesterError(f"Judge reply is missing required key: {key}")
    verdict = str(payload.get("verdict", "")).upper()
    if verdict not in {"PASS", "WARN", "FAIL"}:
        raise TesterError(f"Judge verdict must be PASS, WARN, or FAIL, got: {payload.get('verdict')}")
    payload["verdict"] = verdict
    try:
        payload["score"] = int(payload["score"])
    except Exception as exc:
        raise TesterError("Judge score must be an integer.") from exc
    return payload


def parse_bulleted_lines(block: str) -> list[str]:
    items: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def parse_recommended_edits(block: str) -> list[dict[str, str]]:
    edits: list[dict[str, str]] = []
    for item in parse_bulleted_lines(block):
        parts = [part.strip() for part in item.split("::", 2)]
        if len(parts) == 3:
            edits.append({"section": parts[0], "problem": parts[1], "change": parts[2]})
        else:
            edits.append({"section": "unknown", "problem": item, "change": item})
    return edits


def parse_optimization_marker_reply(reply_text: str) -> dict[str, Any]:
    pattern = re.compile(
        r"SUMMARY:\s*(?P<summary>.*?)\n\s*ROOT_CAUSES:\s*(?P<root_causes>.*?)\n\s*RECOMMENDED_EDITS:\s*(?P<recommended_edits>.*?)\n\s*SUGGESTED_TEST_UPDATES:\s*(?P<suggested_test_updates>.*?)\n\s*UPDATED_SKILL_MD_BEGIN\s*(?P<skill_md>.*?)\s*UPDATED_SKILL_MD_END\s*$",
        flags=re.DOTALL,
    )
    match = pattern.search(reply_text.strip())
    if not match:
        raise TesterError("Optimization reply was neither valid JSON nor the expected marker format.")
    return {
        "summary": match.group("summary").strip(),
        "root_causes": parse_bulleted_lines(match.group("root_causes")),
        "recommended_edits": parse_recommended_edits(match.group("recommended_edits")),
        "suggested_test_updates": parse_bulleted_lines(match.group("suggested_test_updates")),
        "updated_skill_markdown": match.group("skill_md").strip() + "\n",
    }


def parse_minimal_optimization_reply(reply_text: str) -> dict[str, Any]:
    pattern = re.compile(
        r"SUMMARY:\s*(?P<summary>.*?)\n\s*UPDATED_SKILL_MD_BEGIN\s*(?P<skill_md>.*?)\s*UPDATED_SKILL_MD_END\s*$",
        flags=re.DOTALL,
    )
    match = pattern.search(reply_text.strip())
    if not match:
        raise TesterError("Optimization reply did not match the minimal marker format.")
    return {
        "summary": match.group("summary").strip(),
        "root_causes": [],
        "recommended_edits": [],
        "suggested_test_updates": [],
        "updated_skill_markdown": match.group("skill_md").strip() + "\n",
    }


def parse_raw_skill_markdown_reply(reply_text: str) -> dict[str, Any]:
    stripped = reply_text.strip()
    if stripped.startswith("---") and "\nname:" in stripped:
        return {
            "summary": "Model returned a raw revised SKILL.md without structured notes.",
            "root_causes": [],
            "recommended_edits": [],
            "suggested_test_updates": [],
            "updated_skill_markdown": stripped + "\n",
        }
    raise TesterError("Optimization reply was not raw SKILL.md markdown.")


def parse_optimization_reply(reply_text: str) -> dict[str, Any]:
    try:
        return validate_optimization_payload(extract_json_blob(reply_text))
    except Exception:
        pass
    for parser in (
        parse_optimization_marker_reply,
        parse_minimal_optimization_reply,
        parse_raw_skill_markdown_reply,
    ):
        try:
            return parser(reply_text)
        except Exception:
            continue
    raise TesterError("Optimization reply was neither valid JSON nor any supported marker format.")


def request_optimization_reply(
    *,
    repo_root: Path,
    codex_home: Path,
    model: str | None,
    reasoning_effort: str | None,
    sandbox: str,
    approval: str,
    add_dirs: list[Path],
    timeout: int,
    use_rtk: bool,
    primary_message: str,
    fallback_message: str,
    dangerously_bypass_approvals_and_sandbox: bool,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    attempts = [
        ("primary", primary_message),
        ("fallback", fallback_message),
    ]
    errors: list[str] = []
    latest_response: dict[str, Any] | None = None
    latest_reply = ""

    for mode, message in attempts:
        response_payload = run_codex_turn(
            repo_root=repo_root,
            codex_home=codex_home,
            message=message,
            session_id=None,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            approval=approval,
            add_dirs=add_dirs,
            timeout=timeout,
            use_rtk=use_rtk,
            ephemeral=True,
            dangerously_bypass_approvals_and_sandbox=dangerously_bypass_approvals_and_sandbox,
        )
        reply_text = response_payload["reply_text"]
        latest_response = response_payload
        latest_reply = reply_text
        try:
            parsed = parse_optimization_reply(reply_text)
            return response_payload, reply_text, parsed
        except Exception as exc:
            errors.append(f"{mode}: {exc}")

    if latest_response is None:
        raise TesterError("Optimization request did not produce a response.")
    raise TesterError(
        "Optimization reply could not be parsed after primary and fallback attempts: "
        + " | ".join(errors)
        + f". Last raw reply:\n{latest_reply}"
    )


def parse_tab_separated_output(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def init_results_tsv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "round\tfailed_cases\tassertion_failures\tstatus\tdescription\n",
            encoding="utf-8",
        )


def append_results_tsv(
    path: Path,
    *,
    round_index: int,
    failed_cases: int,
    assertion_failures: int,
    status: str,
    description: str,
) -> None:
    init_results_tsv(path)
    safe_description = description.replace("\t", " ").replace("\n", " ").strip()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"{round_index}\t{failed_cases}\t{assertion_failures}\t{status}\t{safe_description}\n"
        )


def load_suite_run_metrics(run_dir: Path) -> dict[str, Any]:
    summary = load_suite_summary(run_dir)
    failed_records = load_failed_case_records(run_dir)
    assertion_failures = sum(len(record.get("failures", [])) for record in failed_records)
    total_cases = len(summary.get("case_results", []))
    failed_cases = int(summary.get("failed_cases", len(failed_records)))
    passed_cases = int(summary.get("passed_cases", max(total_cases - failed_cases, 0)))
    return {
        "failed_cases": failed_cases,
        "assertion_failures": assertion_failures,
        "passed_cases": passed_cases,
        "total_cases": total_cases,
    }


def suite_score(metrics: dict[str, Any]) -> tuple[int, int]:
    return (int(metrics.get("failed_cases", 0)), int(metrics.get("assertion_failures", 0)))


def suite_improved(candidate: dict[str, Any], best: dict[str, Any]) -> bool:
    return suite_score(candidate) < suite_score(best)


def build_self_command_global_args(args: argparse.Namespace) -> list[str]:
    global_args = ["--repo-root", str(expand_path(args.repo_root))]
    if args.codex_home:
        global_args.extend(["--codex-home", str(expand_path(args.codex_home))])
    if args.skills_dir:
        global_args.extend(["--skills-dir", str(expand_path(args.skills_dir))])
    global_args.extend(["--runs-dir", str(expand_path(args.runs_dir))])
    if args.model:
        global_args.extend(["--model", args.model])
    if args.reasoning_effort:
        global_args.extend(["--reasoning-effort", args.reasoning_effort])
    global_args.extend(["--timeout", str(args.timeout), "--sandbox", args.sandbox, "--approval", args.approval])
    for add_dir in args.add_dir:
        global_args.extend(["--add-dir", str(expand_path(add_dir))])
    if args.no_rtk:
        global_args.append("--no-rtk")
    if args.dangerously_bypass_approvals_and_sandbox:
        global_args.append("--dangerously-bypass-approvals-and-sandbox")
    return global_args


def run_self_tool_command(command_args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    script_path = Path(__file__).resolve()
    proc = run_command([sys.executable, str(script_path), *command_args], cwd=cwd)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc


def is_transient_self_tool_failure(proc: subprocess.CompletedProcess[str]) -> bool:
    haystack = f"{proc.stdout}\n{proc.stderr}"
    return "Codex produced no final message." in haystack


def run_self_tool_command_with_retry(
    command_args: list[str],
    cwd: Path,
    *,
    max_attempts: int = 2,
) -> tuple[subprocess.CompletedProcess[str], int]:
    script_path = Path(__file__).resolve()
    attempts: list[subprocess.CompletedProcess[str]] = []

    for _attempt in range(max_attempts):
        proc = run_command([sys.executable, str(script_path), *command_args], cwd=cwd)
        attempts.append(proc)
        if proc.returncode == 0 or not is_transient_self_tool_failure(proc):
            break

    if len(attempts) == 1:
        final_proc = attempts[0]
    else:
        merged_stdout_parts: list[str] = []
        merged_stderr_parts: list[str] = []
        for index, proc in enumerate(attempts, start=1):
            merged_stdout_parts.append(f"=== attempt {index} stdout ===\n{proc.stdout}")
            merged_stderr_parts.append(f"=== attempt {index} stderr ===\n{proc.stderr}")
        last = attempts[-1]
        final_proc = subprocess.CompletedProcess(
            args=last.args,
            returncode=last.returncode,
            stdout="\n".join(merged_stdout_parts),
            stderr="\n".join(merged_stderr_parts),
        )

    if final_proc.stdout:
        print(final_proc.stdout, end="")
    if final_proc.stderr:
        print(final_proc.stderr, end="", file=sys.stderr)
    return final_proc, len(attempts)


def install_command(args: argparse.Namespace) -> int:
    repo_root = expand_path(args.repo_root)
    codex_home = resolve_codex_home(args.codex_home)
    skills_dir = resolve_skills_dir(args.skills_dir, codex_home)
    skill = resolve_skill_spec(args.skill, repo_root=repo_root, install_name=args.install_name)
    destination = install_skill(skill, skills_dir=skills_dir, prune=not args.no_prune)
    skill_info = describe_skill_installation(skill, skills_dir=skills_dir)
    print(f"installed_skill\t{skill.skill_name}")
    print(f"install_name\t{skill.install_name}")
    print(f"source_dir\t{skill.source_dir}")
    print(f"install_dir\t{destination}")
    print(skill_info)
    return 0


def ask_command(args: argparse.Namespace) -> int:
    repo_root = expand_path(args.repo_root)
    codex_home = resolve_codex_home(args.codex_home)
    skills_dir = resolve_skills_dir(args.skills_dir, codex_home)
    runs_dir = expand_path(args.runs_dir)
    prompt = normalize_prompt(args)
    add_dirs = [expand_path(path) for path in args.add_dir]

    destination: Path | None = None
    skill_info_text: str | None = None
    skill_name = args.skill

    if args.skill:
        skill = resolve_skill_spec(args.skill, repo_root=repo_root, install_name=args.install_name)
        skill_name = skill.skill_name
        if not args.no_install:
            destination = install_skill(skill, skills_dir=skills_dir, prune=not args.no_prune)
        else:
            destination = skills_dir / skill.install_name
        skill_info_text = describe_skill_installation(skill, skills_dir=skills_dir)

    message = prompt if args.raw_prompt or not skill_name else build_skill_test_message(skill_name, prompt, destination)
    response_payload = run_codex_turn(
        repo_root=repo_root,
        codex_home=codex_home,
        message=message,
        session_id=args.session_id,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        sandbox=args.sandbox,
        approval=args.approval,
        add_dirs=add_dirs,
        timeout=args.timeout,
        use_rtk=not args.no_rtk,
        ephemeral=args.session_id is None,
        dangerously_bypass_approvals_and_sandbox=args.dangerously_bypass_approvals_and_sandbox,
    )
    reply_text = response_payload["reply_text"]
    meta = response_payload["meta"]

    run_dir = create_run_dir(runs_dir, f"{skill_name or 'codex'}-ask")
    request_payload = {
        "skill_name": skill_name,
        "prompt": prompt,
        "message": message,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "timeout": args.timeout,
        "session_id": args.session_id,
        "sandbox": args.sandbox,
        "approval": args.approval,
    }
    write_single_run_artifacts(run_dir, request_payload, response_payload, reply_text, meta, skill_info_text)

    print(reply_text)
    if args.print_meta:
        print("")
        print(f"[session_id] {meta.get('session_id')}")
        print(f"[model] {meta.get('model') or 'default'}")
        print(f"[run_dir] {run_dir}")
    return 0


def repl_command(args: argparse.Namespace) -> int:
    repo_root = expand_path(args.repo_root)
    codex_home = resolve_codex_home(args.codex_home)
    skills_dir = resolve_skills_dir(args.skills_dir, codex_home)
    runs_dir = expand_path(args.runs_dir)
    add_dirs = [expand_path(path) for path in args.add_dir]

    destination: Path | None = None
    skill_name = args.skill
    if args.skill:
        skill = resolve_skill_spec(args.skill, repo_root=repo_root, install_name=args.install_name)
        skill_name = skill.skill_name
        if not args.no_install:
            destination = install_skill(skill, skills_dir=skills_dir, prune=not args.no_prune)
        else:
            destination = skills_dir / skill.install_name
        describe_skill_installation(skill, skills_dir=skills_dir)

    run_dir = create_run_dir(runs_dir, f"{skill_name or 'codex'}-repl")
    transcript_path = run_dir / "transcript.md"
    turns_path = run_dir / "turns.ndjson"
    transcript_path.write_text("# Codex Skill Test REPL\n\n", encoding="utf-8")

    session_id = args.session_id
    turn_index = 0
    print("Type your prompt and press Enter. Use /exit to stop.")
    if skill_name:
        print(f"Testing skill: {skill_name}")
    if destination:
        print(f"Installed at: {destination}")

    while True:
        try:
            raw_prompt = input("you> ").strip()
        except EOFError:
            print("")
            break

        if not raw_prompt:
            continue
        if raw_prompt in {"/exit", "/quit"}:
            break

        turn_index += 1
        message = raw_prompt if args.raw_prompt or not skill_name else build_skill_test_message(skill_name, raw_prompt, destination)
        response_payload = run_codex_turn(
            repo_root=repo_root,
            codex_home=codex_home,
            message=message,
            session_id=session_id,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            sandbox=args.sandbox,
            approval=args.approval,
            add_dirs=add_dirs,
            timeout=args.timeout,
            use_rtk=not args.no_rtk,
            ephemeral=session_id is None,
            dangerously_bypass_approvals_and_sandbox=args.dangerously_bypass_approvals_and_sandbox,
        )
        reply_text = response_payload["reply_text"]
        meta = response_payload["meta"]
        if meta.get("session_id"):
            session_id = str(meta["session_id"])

        turn_request = {
            "turn": turn_index,
            "skill_name": skill_name,
            "prompt": raw_prompt,
            "message": message,
            "session_id": session_id,
        }
        dump_json_file(run_dir / f"turn-{turn_index:03d}-request.json", turn_request)
        dump_json_file(run_dir / f"turn-{turn_index:03d}-response.json", response_payload)
        with turns_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "turn": turn_index,
                        "prompt": raw_prompt,
                        "reply_text": reply_text,
                        "meta": meta,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(f"## Turn {turn_index}\n\n")
            handle.write(f"**You**\n\n{raw_prompt}\n\n")
            handle.write(f"**Assistant**\n\n{reply_text}\n\n")

        print(f"assistant> {reply_text}")
        print(f"[session_id] {session_id}")

    print(f"saved_run_dir\t{run_dir}")
    return 0


def suite_command(args: argparse.Namespace) -> int:
    repo_root = expand_path(args.repo_root)
    codex_home = resolve_codex_home(args.codex_home)
    skills_dir = resolve_skills_dir(args.skills_dir, codex_home)
    runs_dir = expand_path(args.runs_dir)
    suite_path = expand_path(args.file)
    suite_payload = load_json_file(suite_path)
    add_dirs = [expand_path(path) for path in args.add_dir]

    if not isinstance(suite_payload, dict):
        raise TesterError("Suite file must contain a JSON object.")

    suite_skill = args.skill or suite_payload.get("skill")
    if not suite_skill:
        raise TesterError("Suite requires a skill. Provide --skill or a top-level `skill` key.")

    skill = resolve_skill_spec(str(suite_skill), repo_root=repo_root, install_name=args.install_name)
    if not args.no_install:
        install_skill(skill, skills_dir=skills_dir, prune=not args.no_prune)
    describe_skill_installation(skill, skills_dir=skills_dir)

    cases = suite_payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise TesterError("Suite requires a non-empty `cases` array.")

    suite_run_dir = create_run_dir(runs_dir, f"suite-{skill.install_name}")
    suite_summary: dict[str, Any] = {
        "skill_name": skill.skill_name,
        "suite_file": str(suite_path),
        "case_results": [],
    }
    failed_cases = 0

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise TesterError(f"Suite case #{index} is not an object.")
        case_name = str(case.get("name") or f"case-{index}")
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise TesterError(f"Suite case '{case_name}' requires a non-empty prompt.")

        message = prompt if args.raw_prompt else build_skill_test_message(skill.skill_name, prompt, skills_dir / skill.install_name)
        response_payload = run_codex_turn(
            repo_root=repo_root,
            codex_home=codex_home,
            message=message,
            session_id=None,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            sandbox=args.sandbox,
            approval=args.approval,
            add_dirs=add_dirs,
            timeout=args.timeout,
            use_rtk=not args.no_rtk,
            ephemeral=True,
            dangerously_bypass_approvals_and_sandbox=args.dangerously_bypass_approvals_and_sandbox,
        )
        reply_text = response_payload["reply_text"]
        meta = response_payload["meta"]
        failures = evaluate_case_assertions(reply_text, case)
        if failures:
            failed_cases += 1

        case_dir = suite_run_dir / f"{index:03d}-{slugify(case_name)}"
        case_dir.mkdir(parents=True, exist_ok=False)
        dump_json_file(
            case_dir / "case.json",
            {
                "name": case_name,
                "prompt": prompt,
                "assertions": {
                    key: case[key]
                    for key in (
                        "assert_contains",
                        "assert_not_contains",
                        "assert_regex",
                        "assert_not_regex",
                        "min_reply_chars",
                    )
                    if key in case
                },
            },
        )
        dump_json_file(case_dir / "response.json", response_payload)
        dump_json_file(
            case_dir / "result.json",
            {
                "name": case_name,
                "reply_text": reply_text,
                "meta": meta,
                "failures": failures,
                "passed": not failures,
            },
        )

        suite_summary["case_results"].append(
            {
                "name": case_name,
                "passed": not failures,
                "failures": failures,
                "session_id": meta.get("session_id"),
            }
        )
        status = "PASS" if not failures else "FAIL"
        print(f"[{status}] {case_name}")
        if failures:
            for failure in failures:
                print(f"  - {failure}")

    suite_summary["failed_cases"] = failed_cases
    suite_summary["passed_cases"] = len(cases) - failed_cases
    dump_json_file(suite_run_dir / "suite-summary.json", suite_summary)
    print(f"suite_run_dir\t{suite_run_dir}")

    if failed_cases:
        return EXIT_ASSERTION_FAILED
    return 0


def judge_command(args: argparse.Namespace) -> int:
    repo_root = expand_path(args.repo_root)
    codex_home = resolve_codex_home(args.codex_home)
    runs_dir = expand_path(args.runs_dir)
    add_dirs = [expand_path(path) for path in args.add_dir]

    if args.suite_run_dir:
        suite_run_dir = expand_path(args.suite_run_dir)
    else:
        suite_run_dir = find_latest_suite_run(
            runs_dir,
            only_failed=False,
            skill_name=args.skill_name_filter,
        )

    suite_summary = load_suite_summary(suite_run_dir)
    skill = resolve_skill_spec_from_suite_run(
        suite_run_dir,
        repo_root=repo_root,
        install_name=args.install_name,
        explicit_skill=args.skill,
    )
    skill_markdown = skill.skill_file.read_text(encoding="utf-8")
    case_records = load_case_records(
        suite_run_dir,
        failed_only=False,
        max_reply_chars=args.max_reply_chars,
    )
    if not case_records:
        raise TesterError(f"No case records found under {suite_run_dir}")

    rubric_text = load_default_judge_definition()
    if args.rubric_file:
        rubric_text = expand_path(args.rubric_file).read_text(encoding="utf-8")

    judge_message = build_judge_message(skill, skill_markdown, suite_summary, case_records, rubric_text)
    run_dir = create_run_dir(runs_dir, f"judge-{skill.install_name}")
    response_payload = run_codex_turn(
        repo_root=repo_root,
        codex_home=codex_home,
        message=judge_message,
        session_id=None,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        sandbox=args.sandbox,
        approval=args.approval,
        add_dirs=add_dirs,
        timeout=args.timeout,
        use_rtk=not args.no_rtk,
        ephemeral=True,
        dangerously_bypass_approvals_and_sandbox=args.dangerously_bypass_approvals_and_sandbox,
    )
    reply_text = response_payload["reply_text"]
    try:
        judge_payload = validate_judge_payload(extract_json_blob(reply_text))
    except Exception as exc:
        dump_json_file(
            run_dir / "request.json",
            {
                "skill_name": skill.skill_name,
                "skill_file": str(skill.skill_file),
                "suite_run_dir": str(suite_run_dir),
                "rubric": rubric_text,
                "message": judge_message,
            },
        )
        dump_json_file(run_dir / "response.json", response_payload)
        (run_dir / "raw-reply.txt").write_text(reply_text, encoding="utf-8")
        raise TesterError(f"{exc}. Raw judge reply saved to {run_dir / 'raw-reply.txt'}") from exc

    dump_json_file(
        run_dir / "request.json",
        {
            "skill_name": skill.skill_name,
            "skill_file": str(skill.skill_file),
            "suite_run_dir": str(suite_run_dir),
            "rubric": rubric_text,
            "message": judge_message,
        },
    )
    dump_json_file(run_dir / "response.json", response_payload)
    dump_json_file(run_dir / "judge.json", judge_payload)
    dump_json_file(
        run_dir / "summary.json",
        {
            "meta": response_payload["meta"],
            "suite_run_dir": str(suite_run_dir),
            "verdict": judge_payload.get("verdict"),
            "score": judge_payload.get("score"),
            "should_optimize": judge_payload.get("should_optimize"),
        },
    )

    print(f"skill\t{skill.skill_name}")
    print(f"skill_file\t{skill.skill_file}")
    print(f"suite_run_dir\t{suite_run_dir}")
    print(f"judge_run_dir\t{run_dir}")
    print(f"verdict\t{judge_payload.get('verdict')}")
    print(f"score\t{judge_payload.get('score')}")
    print(f"should_optimize\t{judge_payload.get('should_optimize')}")
    print(f"summary\t{judge_payload.get('summary')}")
    return 0


def optimize_command(args: argparse.Namespace) -> int:
    repo_root = expand_path(args.repo_root)
    codex_home = resolve_codex_home(args.codex_home)
    runs_dir = expand_path(args.runs_dir)
    add_dirs = [expand_path(path) for path in args.add_dir]

    if args.suite_run_dir:
        suite_run_dir = expand_path(args.suite_run_dir)
    else:
        suite_run_dir = find_latest_suite_run(
            runs_dir,
            only_failed=True,
            skill_name=args.skill_name_filter,
        )

    suite_summary = load_suite_summary(suite_run_dir)
    skill = resolve_skill_spec_from_suite_run(
        suite_run_dir,
        repo_root=repo_root,
        install_name=args.install_name,
        explicit_skill=args.skill,
    )
    failed_cases = load_failed_case_records(suite_run_dir)
    if not failed_cases:
        raise TesterError(f"No failed cases found under {suite_run_dir}")

    skill_markdown = skill.skill_file.read_text(encoding="utf-8")
    optimization_prompt = build_skill_optimization_message(skill, skill_markdown, suite_summary, failed_cases)
    run_dir = create_run_dir(runs_dir, f"optimize-{skill.install_name}")
    fallback_prompt = build_skill_raw_rewrite_message(skill, skill_markdown, suite_summary, failed_cases)
    try:
        response_payload, reply_text, optimization_payload = request_optimization_reply(
            repo_root=repo_root,
            codex_home=codex_home,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            sandbox=args.sandbox,
            approval=args.approval,
            add_dirs=add_dirs,
            timeout=args.timeout,
            use_rtk=not args.no_rtk,
            primary_message=optimization_prompt,
            fallback_message=fallback_prompt,
            dangerously_bypass_approvals_and_sandbox=args.dangerously_bypass_approvals_and_sandbox,
        )
    except Exception as exc:
        dump_json_file(
            run_dir / "request.json",
            {
                "skill_name": skill.skill_name,
                "skill_file": str(skill.skill_file),
                "suite_run_dir": str(suite_run_dir),
                "failed_cases": failed_cases,
                "message": optimization_prompt,
                "fallback_message": fallback_prompt,
            },
        )
        if "response_payload" in locals():
            dump_json_file(run_dir / "response.json", response_payload)
        if "reply_text" in locals():
            (run_dir / "raw-reply.txt").write_text(reply_text, encoding="utf-8")
        raise TesterError(f"{exc}. Raw reply saved to {run_dir / 'raw-reply.txt'}") from exc
    meta = response_payload["meta"]
    optimized_markdown = optimization_payload["updated_skill_markdown"].rstrip() + "\n"
    diff_path = render_diff_path(skill.skill_file, repo_root)
    diff_text = make_unified_diff(skill_markdown, optimized_markdown, diff_path)

    dump_json_file(
        run_dir / "request.json",
        {
            "skill_name": skill.skill_name,
            "skill_file": str(skill.skill_file),
            "suite_run_dir": str(suite_run_dir),
            "failed_cases": failed_cases,
            "message": optimization_prompt,
        },
    )
    dump_json_file(run_dir / "response.json", response_payload)
    dump_json_file(run_dir / "optimization.json", optimization_payload)
    dump_json_file(run_dir / "summary.json", {"meta": meta, "suite_run_dir": str(suite_run_dir)})
    (run_dir / "proposed-SKILL.md").write_text(optimized_markdown, encoding="utf-8")
    (run_dir / "skill.diff").write_text(diff_text, encoding="utf-8")

    if args.apply:
        skill.skill_file.write_text(optimized_markdown, encoding="utf-8")
        print(f"applied_skill\t{skill.skill_file}")

    print(f"skill\t{skill.skill_name}")
    print(f"skill_file\t{skill.skill_file}")
    print(f"suite_run_dir\t{suite_run_dir}")
    print(f"optimization_run_dir\t{run_dir}")
    print(f"summary\t{optimization_payload.get('summary')}")
    root_causes = optimization_payload.get("root_causes", [])
    if isinstance(root_causes, list):
        for cause in root_causes:
            print(f"root_cause\t{cause}")
    return 0


def autoloop_command(args: argparse.Namespace) -> int:
    repo_root = expand_path(args.repo_root)
    runs_dir = expand_path(args.runs_dir)
    suite_path = expand_path(args.file)
    suite_payload = load_json_file(suite_path)
    if not isinstance(suite_payload, dict):
        raise TesterError("Suite file must contain a JSON object.")
    if args.max_rounds < 1:
        raise TesterError("Autoloop requires --max-rounds to be at least 1.")
    loop_skill_value = args.skill or suite_payload.get("skill")
    if not isinstance(loop_skill_value, str) or not loop_skill_value.strip():
        raise TesterError("Autoloop requires a skill. Provide --skill or a top-level `skill` key in the suite.")
    skill = resolve_skill_spec(loop_skill_value, repo_root=repo_root, install_name=args.install_name)
    loop_run_dir = create_run_dir(runs_dir, f"autoloop-{skill.install_name}")
    base_args = build_self_command_global_args(args)
    results_tsv_path = loop_run_dir / "results.tsv"
    init_results_tsv(results_tsv_path)
    original_markdown = skill.skill_file.read_text(encoding="utf-8")
    best_markdown = original_markdown
    best_metrics: dict[str, Any] | None = None
    pending_description = "baseline"
    (loop_run_dir / "original-SKILL.md").write_text(original_markdown, encoding="utf-8")

    rounds: list[dict[str, Any]] = []
    final_exit_code = EXIT_ASSERTION_FAILED

    for round_index in range(1, args.max_rounds + 1):
        print(f"[autoloop] suite round {round_index}/{args.max_rounds}")
        suite_args = [*base_args, "suite", "--file", str(suite_path)]
        if args.skill:
            suite_args.extend(["--skill", args.skill])
        if args.install_name:
            suite_args.extend(["--install-name", args.install_name])
        if args.raw_prompt:
            suite_args.append("--raw-prompt")
        if args.no_install:
            suite_args.append("--no-install")
        if args.no_prune:
            suite_args.append("--no-prune")

        suite_proc, suite_attempts = run_self_tool_command_with_retry(suite_args, cwd=repo_root)
        suite_kv = parse_tab_separated_output(suite_proc.stdout)
        round_record: dict[str, Any] = {
            "round": round_index,
            "suite_attempts": suite_attempts,
            "suite_exit_code": suite_proc.returncode,
            "suite_run_dir": suite_kv.get("suite_run_dir"),
        }
        (loop_run_dir / f"round-{round_index:02d}-suite.stdout.txt").write_text(suite_proc.stdout, encoding="utf-8")
        (loop_run_dir / f"round-{round_index:02d}-suite.stderr.txt").write_text(suite_proc.stderr, encoding="utf-8")

        suite_run_dir_text = suite_kv.get("suite_run_dir")
        metrics: dict[str, Any] | None = None
        if suite_run_dir_text:
            metrics = load_suite_run_metrics(Path(suite_run_dir_text))
            round_record["metrics"] = metrics

        if suite_proc.returncode == 0:
            if metrics is not None:
                if best_metrics is None or suite_improved(metrics, best_metrics):
                    best_metrics = metrics
                    best_markdown = skill.skill_file.read_text(encoding="utf-8")
                    round_record["decision"] = "keep"
                else:
                    skill.skill_file.write_text(best_markdown, encoding="utf-8")
                    round_record["decision"] = "discard"
            round_record["status"] = "passed"
            rounds.append(round_record)
            if metrics is not None:
                append_results_tsv(
                    results_tsv_path,
                    round_index=round_index,
                    failed_cases=metrics["failed_cases"],
                    assertion_failures=metrics["assertion_failures"],
                    status=round_record.get("decision", "keep"),
                    description=pending_description,
                )
            final_exit_code = 0
            break

        if suite_proc.returncode != EXIT_ASSERTION_FAILED:
            round_record["status"] = "suite_error"
            round_record["decision"] = "crash"
            rounds.append(round_record)
            append_results_tsv(
                results_tsv_path,
                round_index=round_index,
                failed_cases=metrics["failed_cases"] if metrics else 0,
                assertion_failures=metrics["assertion_failures"] if metrics else 0,
                status="crash",
                description=pending_description or "suite crashed",
            )
            skill.skill_file.write_text(best_markdown, encoding="utf-8")
            final_exit_code = 1
            break

        round_record["status"] = "failed"
        if metrics is not None:
            if best_metrics is None:
                best_metrics = metrics
                best_markdown = skill.skill_file.read_text(encoding="utf-8")
                round_record["decision"] = "keep"
            elif suite_improved(metrics, best_metrics):
                best_metrics = metrics
                best_markdown = skill.skill_file.read_text(encoding="utf-8")
                round_record["decision"] = "keep"
            else:
                skill.skill_file.write_text(best_markdown, encoding="utf-8")
                round_record["decision"] = "discard"
            append_results_tsv(
                results_tsv_path,
                round_index=round_index,
                failed_cases=metrics["failed_cases"],
                assertion_failures=metrics["assertion_failures"],
                status=round_record["decision"],
                description=pending_description,
            )

        if round_index == args.max_rounds:
            round_record["status"] = "failed_no_rounds_left"
            rounds.append(round_record)
            final_exit_code = EXIT_ASSERTION_FAILED
            break

        suite_run_dir = suite_run_dir_text
        if not suite_run_dir:
            raise TesterError("Suite failed but did not print `suite_run_dir`, cannot continue autoloop.")

        print(f"[autoloop] optimize round {round_index}/{args.max_rounds - 1}")
        optimize_args = [*base_args, "optimize", "--suite-run-dir", suite_run_dir, "--apply"]
        if args.skill:
            optimize_args.extend(["--skill", args.skill])
        if args.skill_name_filter:
            optimize_args.extend(["--skill-name-filter", args.skill_name_filter])
        if args.install_name:
            optimize_args.extend(["--install-name", args.install_name])

        optimize_proc, optimize_attempts = run_self_tool_command_with_retry(optimize_args, cwd=repo_root)
        optimize_kv = parse_tab_separated_output(optimize_proc.stdout)
        round_record["optimization_attempts"] = optimize_attempts
        round_record["optimization_exit_code"] = optimize_proc.returncode
        round_record["optimization_run_dir"] = optimize_kv.get("optimization_run_dir")
        round_record["optimization_summary"] = optimize_kv.get("summary")
        (loop_run_dir / f"round-{round_index:02d}-optimize.stdout.txt").write_text(
            optimize_proc.stdout,
            encoding="utf-8",
        )
        (loop_run_dir / f"round-{round_index:02d}-optimize.stderr.txt").write_text(
            optimize_proc.stderr,
            encoding="utf-8",
        )

        rounds.append(round_record)
        if optimize_proc.returncode != 0:
            skill.skill_file.write_text(best_markdown, encoding="utf-8")
            final_exit_code = 1
            break
        if optimize_kv.get("summary"):
            pending_description = optimize_kv["summary"]

    summary_payload = {
        "suite_file": str(suite_path),
        "skill": loop_skill_value,
        "max_rounds": args.max_rounds,
        "final_exit_code": final_exit_code,
        "best_metrics": best_metrics,
        "rounds": rounds,
    }
    dump_json_file(loop_run_dir / "autoloop-summary.json", summary_payload)
    (loop_run_dir / "best-SKILL.md").write_text(best_markdown, encoding="utf-8")
    skill.skill_file.write_text(best_markdown, encoding="utf-8")
    print(f"autoloop_run_dir\t{loop_run_dir}")
    print(f"autoloop_final_exit_code\t{final_exit_code}")
    return final_exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="Repository root used to resolve skill paths")
    parser.add_argument("--codex-home", help="Codex home directory (defaults to $CODEX_HOME or ~/.codex)")
    parser.add_argument(
        "--skills-dir",
        "--workspace-skills-dir",
        dest="skills_dir",
        help="Codex skills directory (defaults to <codex-home>/skills)",
    )
    parser.add_argument(
        "--runs-dir",
        default=str(DEFAULT_RUNS_DIR),
        help="Directory used to save transcripts and raw responses",
    )
    parser.add_argument("--model", help="Codex model id")
    parser.add_argument(
        "--reasoning-effort",
        "--thinking",
        dest="reasoning_effort",
        default=DEFAULT_REASONING_EFFORT,
        help="Codex model reasoning effort",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Maximum seconds to wait for each Codex run",
    )
    parser.add_argument("--sandbox", default=DEFAULT_SANDBOX, help="Codex sandbox mode")
    parser.add_argument("--approval", default=DEFAULT_APPROVAL, help="Codex approval policy")
    parser.add_argument(
        "--dangerously-bypass-approvals-and-sandbox",
        action="store_true",
        dest="dangerously_bypass_approvals_and_sandbox",
        help="Pass Codex's full bypass flag instead of sandbox + approval options",
    )
    parser.add_argument("--add-dir", action="append", default=[], help="Additional writable directory for Codex")
    parser.add_argument("--no-rtk", action="store_true", help="Call `codex` directly instead of `rtk codex`")

    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="Install a skill into the active Codex skills directory")
    install_parser.add_argument("--skill", required=True, help="Skill directory, SKILL.md path, or skill name")
    install_parser.add_argument("--install-name", help="Override the installed skill directory name")
    install_parser.add_argument("--no-prune", action="store_true", help="Do not delete the existing install directory first")
    install_parser.set_defaults(handler=install_command)

    sync_parser = subparsers.add_parser("sync", help="Deprecated alias for `install`")
    sync_parser.add_argument("--skill", required=True, help="Skill directory, SKILL.md path, or skill name")
    sync_parser.add_argument("--install-name", help="Override the installed skill directory name")
    sync_parser.add_argument("--no-prune", action="store_true", help="Do not delete the existing install directory first")
    sync_parser.set_defaults(handler=install_command)

    ask_parser = subparsers.add_parser("ask", help="Run a single Codex skill test prompt")
    ask_parser.add_argument("--skill", help="Skill directory, SKILL.md path, or skill name")
    ask_parser.add_argument("--install-name", help="Override the installed skill directory name")
    ask_parser.add_argument("--prompt", help="Prompt text to send")
    ask_parser.add_argument("--prompt-file", help="Read prompt text from a file")
    ask_parser.add_argument("--session-id", help="Continue an existing Codex session")
    ask_parser.add_argument("--raw-prompt", action="store_true", help="Send the prompt as-is without skill test framing")
    ask_parser.add_argument("--no-install", "--no-sync", dest="no_install", action="store_true", help="Do not install the skill before testing")
    ask_parser.add_argument("--no-prune", action="store_true", help="Do not delete the existing install directory first")
    ask_parser.add_argument("--print-meta", action="store_true", help="Print session id, model, and saved run directory")
    ask_parser.set_defaults(handler=ask_command)

    repl_parser = subparsers.add_parser("repl", help="Start an interactive multi-turn skill test session")
    repl_parser.add_argument("--skill", help="Skill directory, SKILL.md path, or skill name")
    repl_parser.add_argument("--install-name", help="Override the installed skill directory name")
    repl_parser.add_argument("--session-id", help="Continue an existing Codex session")
    repl_parser.add_argument("--raw-prompt", action="store_true", help="Send each prompt as-is without skill framing")
    repl_parser.add_argument("--no-install", "--no-sync", dest="no_install", action="store_true", help="Do not install the skill before testing")
    repl_parser.add_argument("--no-prune", action="store_true", help="Do not delete the existing install directory first")
    repl_parser.set_defaults(handler=repl_command)

    suite_parser = subparsers.add_parser("suite", help="Run a JSON suite of skill regression cases")
    suite_parser.add_argument("--file", required=True, help="Suite JSON file")
    suite_parser.add_argument("--skill", help="Override the suite's top-level skill")
    suite_parser.add_argument("--install-name", help="Override the installed skill directory name")
    suite_parser.add_argument("--raw-prompt", action="store_true", help="Send prompts as-is without skill framing")
    suite_parser.add_argument("--no-install", "--no-sync", dest="no_install", action="store_true", help="Do not install the skill before running the suite")
    suite_parser.add_argument("--no-prune", action="store_true", help="Do not delete the existing install directory first")
    suite_parser.set_defaults(handler=suite_command)

    judge_parser = subparsers.add_parser("judge", help="Ask an independent Codex judge agent to review a suite run")
    judge_parser.add_argument("--suite-run-dir", help="Use a specific suite run directory")
    judge_parser.add_argument("--skill", help="Explicit skill path or name to judge")
    judge_parser.add_argument("--skill-name-filter", help="Only consider suite runs for this skill name")
    judge_parser.add_argument("--install-name", help="Override the installed skill directory name")
    judge_parser.add_argument("--rubric-file", help="Read judge rubric/definition text from a file")
    judge_parser.add_argument(
        "--max-reply-chars",
        type=int,
        default=6000,
        help="Maximum reply characters from each case to include in the judge prompt; use 0 for full replies",
    )
    judge_parser.set_defaults(handler=judge_command)

    optimize_parser = subparsers.add_parser("optimize", help="Analyze the latest failed suite and suggest SKILL.md improvements")
    optimize_parser.add_argument("--skill", help="Explicit skill path or name to optimize")
    optimize_parser.add_argument("--skill-name-filter", help="Only consider failed suite runs for this skill name")
    optimize_parser.add_argument("--install-name", help="Override the installed skill directory name")
    optimize_parser.add_argument("--suite-run-dir", help="Use a specific failed suite run directory")
    optimize_parser.add_argument("--apply", action="store_true", help="Overwrite the source SKILL.md with the proposed markdown")
    optimize_parser.set_defaults(handler=optimize_command)

    autoloop_parser = subparsers.add_parser("autoloop", help="Run an install -> test -> optimize -> retest loop for a skill")
    autoloop_parser.add_argument("--file", required=True, help="Suite JSON file")
    autoloop_parser.add_argument("--skill", help="Explicit skill path or name to optimize during the loop")
    autoloop_parser.add_argument("--skill-name-filter", help="Skill name filter used when locating failed suite runs")
    autoloop_parser.add_argument("--install-name", help="Override the installed skill directory name")
    autoloop_parser.add_argument("--max-rounds", type=int, default=3, help="Maximum suite attempts to run")
    autoloop_parser.add_argument("--raw-prompt", action="store_true", help="Send prompts as-is without skill framing")
    autoloop_parser.add_argument("--no-install", "--no-sync", dest="no_install", action="store_true", help="Do not install the skill before each suite run")
    autoloop_parser.add_argument("--no-prune", action="store_true", help="Do not delete the existing install directory first")
    autoloop_parser.set_defaults(handler=autoloop_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except TesterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

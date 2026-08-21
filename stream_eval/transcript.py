"""Normalize agent JSONL transcripts into portable eval actions"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from stream_eval.agents import DEFAULT_AGENT


@dataclass
class ToolUse:
    name: str
    input: dict


@dataclass
class Action:
    name: str
    input: dict


@dataclass
class Artifact:
    path: str
    content: str


@dataclass
class ParsedTranscript:
    actions: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    tool_uses: list = field(default_factory=list)
    final_text: str | None = None
    transcript_path: Path | None = None

    @property
    def first_action(self):
        return self.actions[0] if self.actions else None

    @property
    def first_skill(self):
        first = self.first_action
        if first is not None and first.name == "skill":
            return first.input.get("skill")
        return None


_CLAUDE_ACTIONS = {
    "Skill": "skill",
    "Bash": "command",
    "Read": "read",
    "Write": "file_change",
    "Edit": "file_change",
    "WebFetch": "web_fetch",
    "Agent": "agent",
}

_OPENCODE_ACTIONS = {
    "skill": "skill",
    "bash": "command",
    "read": "read",
    "write": "file_change",
    "edit": "file_change",
    "apply_patch": "file_change",
    "webfetch": "web_fetch",
    "task": "agent",
}

_SCOPED_SKILL_RE = re.compile(
    r"(?:^|[/\\])(?:\.agents|\.claude|\.codex)[/\\]skills"
    r"[/\\]([^/\\\s'\"]+)[/\\]SKILL\.md"
)


def parse_transcript(path, *, agent=DEFAULT_AGENT, artifacts=(), known_skills=()):
    out = ParsedTranscript(transcript_path=Path(path))
    out.artifacts = [
        Artifact(path=item.get("path", ""), content=item.get("content", ""))
        for item in artifacts
        if isinstance(item, dict)
    ]
    with open(path) as transcript:
        for raw in transcript:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if agent == "claude":
                _parse_claude_event(event, out)
            elif agent == "codex":
                _parse_codex_event(event, out, known_skills=known_skills)
            elif agent == "opencode":
                _parse_opencode_event(event, out)
            else:
                raise ValueError(f"unsupported transcript agent {agent!r}")
    return out


def _parse_claude_event(event, out):
    event_type = event.get("type")
    if event_type == "assistant":
        for content in event.get("message", {}).get("content", []):
            if content.get("type") != "tool_use":
                continue
            name = content.get("name", "")
            native_input = content.get("input", {}) or {}
            out.tool_uses.append(ToolUse(name=name, input=native_input))
            out.actions.append(Action(
                name=_CLAUDE_ACTIONS.get(name, _snake_case(name)),
                input=_normalize_claude_input(name, native_input),
            ))
            if name == "Write":
                _append_inline_artifact(
                    out,
                    path=native_input.get("file_path"),
                    content=native_input.get("content"),
                )
    elif event_type == "result":
        result = event.get("result")
        out.final_text = result if isinstance(result, str) else str(result)


def _parse_codex_event(event, out, *, known_skills):
    if event.get("type") != "item.completed":
        return
    item = event.get("item", {}) or {}
    item_type = item.get("type")
    if item_type == "agent_message":
        text = item.get("text")
        if text is not None:
            out.final_text = text if isinstance(text, str) else str(text)
        return
    if item_type == "command_execution":
        command = item.get("command", "")
        skill = _skill_from_command(command, known_skills=known_skills)
        if skill:
            out.actions.append(Action(name="skill", input={"skill": skill}))
        native_input = {
            "command": command,
            "output": item.get("aggregated_output", ""),
            "exit_code": item.get("exit_code"),
        }
        out.tool_uses.append(ToolUse(
            name="command_execution",
            input=native_input,
        ))
        out.actions.append(Action(name="command", input=native_input))
        return
    if item_type == "file_change":
        native_input = {"changes": item.get("changes", [])}
        out.tool_uses.append(ToolUse(name="file_change", input=native_input))
        out.actions.append(Action(name="file_change", input=native_input))
        return
    if item_type == "mcp_tool_call":
        native_input = {
            "server": item.get("server"),
            "tool": item.get("tool"),
            "arguments": item.get("arguments", {}),
        }
        out.tool_uses.append(ToolUse(name="mcp_tool_call", input=native_input))
        out.actions.append(Action(name="mcp", input=native_input))


def _parse_opencode_event(event, out):
    event_type = event.get("type")
    part = event.get("part", {}) or {}
    if event_type == "text":
        text = part.get("text")
        if text is not None:
            out.final_text = text if isinstance(text, str) else str(text)
        return
    if event_type != "tool_use":
        return

    name = part.get("tool", "")
    state = part.get("state", {}) or {}
    native_input = state.get("input", {}) or {}
    if not isinstance(native_input, dict):
        native_input = {}
    out.tool_uses.append(ToolUse(name=name, input=native_input))
    out.actions.append(Action(
        name=_OPENCODE_ACTIONS.get(name, _snake_case(name)),
        input=_normalize_opencode_input(name, native_input),
    ))
    if name == "write":
        _append_inline_artifact(
            out,
            path=native_input.get("filePath"),
            content=native_input.get("content"),
        )


def _normalize_claude_input(name, native_input):
    if name == "Read":
        return {
            "path": native_input.get("file_path", ""),
            **native_input,
        }
    if name in ("Write", "Edit"):
        return {
            "path": native_input.get("file_path", ""),
            **native_input,
        }
    return dict(native_input)


def _normalize_opencode_input(name, native_input):
    if name == "skill":
        return {
            "skill": native_input.get("name", ""),
            **native_input,
        }
    if name in ("read", "write", "edit"):
        return {
            "path": native_input.get("filePath", ""),
            **native_input,
        }
    return dict(native_input)


def _append_inline_artifact(out, *, path, content):
    if not isinstance(path, str) or not path:
        return
    if not isinstance(content, str):
        return
    if any(artifact.path == path for artifact in out.artifacts):
        return
    out.artifacts.append(Artifact(path=path, content=content))


def _skill_from_command(command, *, known_skills):
    if not isinstance(command, str) or "SKILL.md" not in command:
        return None
    match = _SCOPED_SKILL_RE.search(command)
    if match:
        return match.group(1)
    for skill in known_skills:
        if re.search(
            rf"(?:^|[/\\]){re.escape(skill)}[/\\]SKILL\.md(?:$|[\s'\"])",
            command,
        ):
            return skill
    return None


def _snake_case(value):
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", value or "")
    return value.replace("-", "_").lower()

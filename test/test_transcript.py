"""Tests for portable transcript normalization"""
import json

from stream_eval.transcript import parse_transcript


def _write_jsonl(path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def test_claude_tools_normalize_to_portable_actions(tmp_path):
    transcript = _write_jsonl(tmp_path / "claude.jsonl", [
        {
            "type": "assistant",
            "message": {"content": [
                {
                    "type": "tool_use",
                    "name": "Skill",
                    "input": {"skill": "demo-skill"},
                },
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "printf demo"},
                },
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {
                        "file_path": "demo.txt",
                        "content": "demo",
                    },
                },
            ]},
        },
        {"type": "result", "result": "finished"},
    ])

    parsed = parse_transcript(transcript, agent="claude")

    assert [action.name for action in parsed.actions] == [
        "skill", "command", "file_change",
    ]
    assert parsed.first_skill == "demo-skill"
    assert parsed.actions[2].input["path"] == "demo.txt"
    assert parsed.artifacts[0].path == "demo.txt"
    assert parsed.artifacts[0].content == "demo"
    assert parsed.final_text == "finished"


def test_codex_skill_read_normalizes_before_command_action(tmp_path):
    transcript = _write_jsonl(tmp_path / "codex.jsonl", [
        {"type": "thread.started", "thread_id": "thread-1"},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": (
                    "/bin/bash -lc 'sed -n 1,120p "
                    "/home/test/.agents/skills/demo-skill/SKILL.md'"
                ),
                "aggregated_output": "skill body",
                "exit_code": 0,
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "finished"},
        },
    ])

    parsed = parse_transcript(
        transcript,
        agent="codex",
        known_skills=("demo-skill",),
    )

    assert [action.name for action in parsed.actions] == ["skill", "command"]
    assert parsed.first_skill == "demo-skill"
    assert parsed.tool_uses[0].name == "command_execution"
    assert parsed.final_text == "finished"


def test_codex_source_skill_path_uses_expected_skill_hint(tmp_path):
    transcript = _write_jsonl(tmp_path / "codex-source.jsonl", [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "sed -n '1,80p' /repo/skills/demo-skill/SKILL.md",
                "aggregated_output": "skill body",
                "exit_code": 0,
            },
        },
    ])

    parsed = parse_transcript(
        transcript,
        agent="codex",
        known_skills=("demo-skill",),
    )

    assert parsed.first_skill == "demo-skill"


def test_codex_file_change_and_artifact_normalize(tmp_path):
    transcript = _write_jsonl(tmp_path / "codex-write.jsonl", [
        {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [{"path": "demo.sh", "kind": "add"}],
            },
        },
    ])

    parsed = parse_transcript(
        transcript,
        agent="codex",
        artifacts=({"path": "demo.sh", "content": "#!/bin/sh\n"},),
    )

    assert parsed.actions[0].name == "file_change"
    assert parsed.artifacts[0].path == "demo.sh"
    assert parsed.artifacts[0].content == "#!/bin/sh\n"


def test_unknown_codex_skill_read_is_not_mistaken_for_activation(tmp_path):
    transcript = _write_jsonl(tmp_path / "codex-other.jsonl", [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "sed -n '1,80p' /repo/skills/other/SKILL.md",
                "aggregated_output": "other skill body",
                "exit_code": 0,
            },
        },
    ])

    parsed = parse_transcript(
        transcript,
        agent="codex",
        known_skills=("demo-skill",),
    )

    assert [action.name for action in parsed.actions] == ["command"]
    assert parsed.first_skill is None


def test_opencode_tools_normalize_to_portable_actions(tmp_path):
    transcript = _write_jsonl(tmp_path / "opencode.jsonl", [
        {
            "type": "tool_use",
            "timestamp": 1,
            "sessionID": "session-1",
            "part": {
                "type": "tool",
                "tool": "skill",
                "state": {
                    "status": "completed",
                    "input": {"name": "demo-skill"},
                    "output": "skill body",
                },
            },
        },
        {
            "type": "tool_use",
            "timestamp": 2,
            "sessionID": "session-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "printf demo"},
                    "output": "demo",
                },
            },
        },
        {
            "type": "tool_use",
            "timestamp": 3,
            "sessionID": "session-1",
            "part": {
                "type": "tool",
                "tool": "write",
                "state": {
                    "status": "completed",
                    "input": {
                        "filePath": "/work/demo.txt",
                        "content": "demo",
                    },
                    "output": "Wrote file successfully",
                },
            },
        },
        {
            "type": "text",
            "timestamp": 4,
            "sessionID": "session-1",
            "part": {"type": "text", "text": "finished"},
        },
    ])

    parsed = parse_transcript(transcript, agent="opencode")

    assert [action.name for action in parsed.actions] == [
        "skill", "command", "file_change",
    ]
    assert parsed.first_skill == "demo-skill"
    assert parsed.actions[2].input["path"] == "/work/demo.txt"
    assert parsed.artifacts[0].path == "/work/demo.txt"
    assert parsed.artifacts[0].content == "demo"
    assert parsed.tool_uses[0].name == "skill"
    assert parsed.final_text == "finished"


def test_opencode_read_and_task_normalize(tmp_path):
    transcript = _write_jsonl(tmp_path / "opencode-other.jsonl", [
        {
            "type": "tool_use",
            "part": {
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "/work/README.md"},
                },
            },
        },
        {
            "type": "tool_use",
            "part": {
                "tool": "task",
                "state": {
                    "status": "completed",
                    "input": {"description": "delegate"},
                },
            },
        },
    ])

    parsed = parse_transcript(transcript, agent="opencode")

    assert [action.name for action in parsed.actions] == ["read", "agent"]
    assert parsed.actions[0].input["path"] == "/work/README.md"

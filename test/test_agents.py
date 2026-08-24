"""Tests for agent-specific command and event adapters"""
import json
from pathlib import Path

import pytest

from stream_eval.agents import agent_for_executable, get_agent_adapter


def test_claude_isolated_command_uses_stream_json_and_restrictions():
    command = get_agent_adapter("claude").build_command(
        query="test query",
        model=None,
        effort="high",
        profile="isolated",
    )

    assert command[:3] == ["claude", "-p", "test query"]
    assert ["--model", "sonnet"] == command[
        command.index("--model"):command.index("--model") + 2
    ]
    assert ["--effort", "high"] == command[
        command.index("--effort"):command.index("--effort") + 2
    ]
    assert "--strict-mcp-config" in command
    assert "--disallowedTools" in command


def test_claude_inherit_command_keeps_user_integrations():
    command = get_agent_adapter("claude").build_command(
        query="test query",
        model="claude-test-model",
        effort=None,
        profile="inherit",
    )

    assert "--strict-mcp-config" not in command
    assert "--disallowedTools" not in command
    assert command[-2:] == ["--permission-mode", "bypassPermissions"]


def test_codex_isolated_command_uses_json_and_ignores_user_config():
    command = get_agent_adapter("codex").build_command(
        query="test query",
        model="gpt-test",
        effort="high",
        profile="isolated",
    )

    assert command[:3] == ["codex", "exec", "--json"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "agents.enabled=false" in command
    assert "model_reasoning_effort=\"high\"" in command
    assert command[-1] == "test query"


def test_codex_inherit_command_keeps_user_config():
    command = get_agent_adapter("codex").build_command(
        query="test query",
        model=None,
        effort=None,
        profile="inherit",
    )

    assert "--ignore-user-config" not in command
    assert "--ignore-rules" not in command
    assert "agents.enabled=false" not in command
    assert "--model" not in command


def test_codex_isolated_environment_separates_auth_from_user_skills(
    tmp_path,
):
    real_home = tmp_path / "real-home"
    source_codex_home = tmp_path / "source-codex"
    source_auth = source_codex_home / "auth.json"
    source_auth.parent.mkdir(parents=True)
    source_auth.write_text('{"test":"credential"}')
    (source_codex_home / "skills" / "unrelated").mkdir(parents=True)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()

    adapter = get_agent_adapter("codex")
    with adapter.prepare_environment(
        {
            "HOME": str(real_home),
            "CODEX_HOME": str(source_codex_home),
            "PATH": "/bin",
        },
        profile="isolated",
        isolated_home=isolated_home,
    ) as env:
        isolated_codex_home = isolated_home / ".codex"
        assert env["HOME"] == str(isolated_home)
        assert env["CODEX_HOME"] == str(isolated_codex_home)
        assert env["PATH"] == "/bin"
        isolated_auth = isolated_codex_home / "auth.json"
        assert isolated_auth.is_symlink()
        assert isolated_auth.resolve() == source_auth.resolve()
        assert not (isolated_codex_home / "skills").exists()


def test_opencode_isolated_command_uses_json_and_pure_mode():
    command = get_agent_adapter("opencode").build_command(
        query="test query",
        model="anthropic/test-model",
        effort="high",
        profile="isolated",
    )

    assert command[:5] == [
        "opencode", "run", "--format", "json", "--auto",
    ]
    assert "--pure" in command
    assert ["--model", "anthropic/test-model"] == command[
        command.index("--model"):command.index("--model") + 2
    ]
    assert ["--variant", "high"] == command[
        command.index("--variant"):command.index("--variant") + 2
    ]
    assert command[-1] == "test query"


def test_opencode_inherit_command_keeps_external_plugins():
    command = get_agent_adapter("opencode").build_command(
        query="test query",
        model=None,
        effort=None,
        profile="inherit",
    )

    assert "--pure" not in command


def test_opencode_isolated_environment_routes_state_and_preserves_auth(
    tmp_path,
):
    real_home = tmp_path / "real-home"
    source_data = tmp_path / "source-data"
    source_auth = source_data / "opencode" / "auth.json"
    source_auth.parent.mkdir(parents=True)
    source_auth.write_text('{"test":"credential"}')
    isolated_home = tmp_path / "isolated-home"
    (isolated_home / ".opencode").mkdir(parents=True)

    adapter = get_agent_adapter("opencode")
    with adapter.prepare_environment(
        {
            "HOME": str(real_home),
            "XDG_DATA_HOME": str(source_data),
            "OPENCODE_CONFIG": "/untrusted/config.json",
            "OPENCODE_CONFIG_CONTENT": '{"plugin":["untrusted"]}',
            "PATH": "/bin",
        },
        profile="isolated",
        isolated_home=isolated_home,
    ) as env:
        assert env["HOME"] == str(isolated_home)
        assert env["OPENCODE_CONFIG_DIR"] == str(
            isolated_home / ".opencode"
        )
        assert env["OPENCODE_PURE"] == "1"
        assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
        assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
        assert env["PATH"] == "/bin"
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config["mcp"] == {}
        assert config["permission"]["task"] == "deny"
        assert config["permission"]["skill"] == {
            "*": "allow",
            "customize-opencode": "deny",
        }
        copied_auth = (
            Path(env["XDG_DATA_HOME"]) / "opencode" / "auth.json"
        )
        assert copied_auth.is_symlink()
        assert copied_auth.resolve() == source_auth.resolve()


def test_opencode_restricted_environment_exposes_standard_global_skills(
    tmp_path,
):
    real_home = tmp_path / "real-home"
    agents_skills = real_home / ".agents" / "skills"
    claude_skills = real_home / ".claude" / "skills"
    agents_skills.mkdir(parents=True)
    claude_skills.mkdir(parents=True)

    adapter = get_agent_adapter("opencode")
    with adapter.prepare_environment(
        {"HOME": str(real_home)},
        profile="restricted",
    ) as env:
        runtime_root = Path(env["OPENCODE_CONFIG_DIR"]).parent
        assert runtime_root.exists()
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config["skills"]["paths"] == [
            str(agents_skills),
            str(claude_skills),
        ]
    assert not runtime_root.exists()


@pytest.mark.parametrize(
    ("agent", "removed", "retained"),
    [
        ("claude", "CLAUDECODE", "CODEX_THREAD_ID"),
        ("codex", "CODEX_THREAD_ID", "CLAUDECODE"),
    ],
)
def test_adapter_removes_only_its_nested_session_marker(
    agent, removed, retained,
):
    clean = get_agent_adapter(agent).sanitize_environment({
        removed: "nested",
        retained: "other-agent",
        "PATH": "/bin",
    })

    assert removed not in clean
    assert clean[retained] == "other-agent"
    assert clean["PATH"] == "/bin"


def test_claude_classifier_reports_retry_budget_fields():
    kind, info = get_agent_adapter("claude").classify_event({
        "type": "system",
        "subtype": "api_retry",
        "attempt": 2,
        "max_retries": 7,
    })

    assert kind == "retry"
    assert info == {"attempt": 2, "max_retries": 7}


def test_codex_classifier_treats_completed_items_as_output():
    kind, info = get_agent_adapter("codex").classify_event({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "done"},
    })

    assert kind == "output"
    assert info is None


def test_opencode_classifier_treats_tool_events_as_output():
    kind, info = get_agent_adapter("opencode").classify_event({
        "type": "tool_use",
        "part": {"tool": "skill"},
    })

    assert kind == "output"
    assert info is None


@pytest.mark.parametrize(
    ("executable", "expected"),
    [
        ("claude", "claude"),
        ("/usr/local/bin/claude", "claude"),
        ("codex", "codex"),
        ("/opt/homebrew/bin/codex", "codex"),
        ("opencode", "opencode"),
        ("/usr/local/bin/opencode", "opencode"),
        ("git", None),
    ],
)
def test_agent_for_executable_uses_exact_basename(executable, expected):
    assert agent_for_executable(executable) == expected


def test_unknown_agent_is_rejected():
    with pytest.raises(ValueError, match="unknown agent"):
        get_agent_adapter("other")

"""Agent CLI adapters for stream-eval

The runner owns scheduling and worktree isolation. Adapters own only the
host-specific command line, environment boundary, and retry event shape.
"""
import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_AGENTS = ("claude", "codex", "opencode")
DEFAULT_AGENT = "claude"
SUPPORTED_PROFILES = ("isolated", "restricted", "inherit")


_CLAUDE_STRIP_FLAGS = (
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}',
    "--disallowedTools", "Agent",
)


@dataclass(frozen=True)
class AgentAdapter:
    name: str
    executable: str
    default_model: str | None

    def build_command(self, *, query, model, effort, profile):
        if profile not in SUPPORTED_PROFILES:
            raise ValueError(
                f"unknown profile {profile!r}; "
                f"must be one of {list(SUPPORTED_PROFILES)!r}"
            )
        if self.name == "claude":
            return _claude_command(
                query=query,
                model=model or self.default_model,
                effort=effort,
                profile=profile,
            )
        if self.name == "codex":
            return _codex_command(
                query=query,
                model=model,
                effort=effort,
                profile=profile,
            )
        if self.name == "opencode":
            return _opencode_command(
                query=query,
                model=model,
                effort=effort,
                profile=profile,
            )
        raise ValueError(f"unsupported agent adapter {self.name!r}")

    def sanitize_environment(self, env):
        clean = dict(env)
        if self.name == "claude":
            clean.pop("CLAUDECODE", None)
        elif self.name == "codex":
            clean.pop("CODEX_THREAD_ID", None)
        return clean

    @contextlib.contextmanager
    def prepare_environment(self, env, *, profile, isolated_home=None):
        clean = self.sanitize_environment(env)
        real_home = Path(clean.get("HOME") or os.path.expanduser("~"))
        if isolated_home is not None:
            if self.name == "codex":
                source_codex_home = Path(
                    clean.get("CODEX_HOME") or real_home / ".codex"
                ).expanduser().resolve()
                isolated_codex_home = Path(isolated_home) / ".codex"
                isolated_codex_home.mkdir(parents=True, exist_ok=True)
                source_auth = source_codex_home / "auth.json"
                isolated_auth = isolated_codex_home / "auth.json"
                if source_auth.is_file() and not isolated_auth.exists():
                    os.symlink(source_auth, isolated_auth)
                clean["CODEX_HOME"] = str(isolated_codex_home)
            clean["HOME"] = str(isolated_home)
        if self.name != "opencode" or profile == "inherit":
            yield clean
            return
        with _prepare_opencode_environment(
            clean,
            profile=profile,
            isolated_home=isolated_home,
            real_home=real_home,
        ) as prepared:
            yield prepared

    def classify_event(self, event):
        if not isinstance(event, dict):
            return None, None
        if self.name == "claude":
            if (
                event.get("type") == "system"
                and event.get("subtype") == "api_retry"
            ):
                return "retry", {
                    "attempt": event.get("attempt", 0),
                    "max_retries": event.get("max_retries", 0),
                }
            if event.get("type") in ("assistant", "user", "result"):
                return "output", None
            return None, None
        if self.name == "codex":
            if event.get("type") in (
                "item.started",
                "item.completed",
                "turn.completed",
                "turn.failed",
                "error",
            ):
                return "output", None
            return None, None
        if self.name == "opencode":
            if event.get("type") in (
                "text",
                "tool_use",
                "step_start",
                "step_finish",
                "reasoning",
                "error",
            ):
                return "output", None
            return None, None
        return None, None


_ADAPTERS = {
    "claude": AgentAdapter(
        name="claude",
        executable="claude",
        default_model="sonnet",
    ),
    "codex": AgentAdapter(
        name="codex",
        executable="codex",
        default_model=None,
    ),
    "opencode": AgentAdapter(
        name="opencode",
        executable="opencode",
        default_model=None,
    ),
}


def get_agent_adapter(name):
    try:
        return _ADAPTERS[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown agent {name!r}; must be one of {list(SUPPORTED_AGENTS)!r}"
        ) from exc


def agent_for_executable(value):
    name = value.rsplit("/", 1)[-1]
    for adapter in _ADAPTERS.values():
        if name == adapter.executable:
            return adapter.name
    return None


def _claude_command(*, query, model, effort, profile):
    command = [
        "claude",
        "-p", query,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    if model:
        command.extend(("--model", model))
    if effort:
        command.extend(("--effort", effort))
    command.extend(("--permission-mode", "bypassPermissions"))
    if profile in ("isolated", "restricted"):
        command.extend(_CLAUDE_STRIP_FLAGS)
    return command


def _codex_command(*, query, model, effort, profile):
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if profile in ("isolated", "restricted"):
        command.extend((
            "--ignore-user-config",
            "--ignore-rules",
            "-c", "agents.enabled=false",
        ))
    if model:
        command.extend(("--model", model))
    if effort:
        command.extend((
            "-c", f"model_reasoning_effort={json.dumps(effort)}",
        ))
    command.append(query)
    return command


def _opencode_command(*, query, model, effort, profile):
    command = ["opencode", "run", "--format", "json", "--auto"]
    if profile in ("isolated", "restricted"):
        command.append("--pure")
    if model:
        command.extend(("--model", model))
    if effort:
        command.extend(("--variant", effort))
    command.append(query)
    return command


_OPENCODE_ROUTING_ENV = (
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_DB",
)


@contextlib.contextmanager
def _prepare_opencode_environment(
    env, *, profile, isolated_home, real_home,
):
    clean = dict(env)
    for name in _OPENCODE_ROUTING_ENV:
        clean.pop(name, None)

    if isolated_home is None:
        runtime_ctx = tempfile.TemporaryDirectory(
            prefix="stream-eval-opencode-",
        )
    else:
        runtime_ctx = contextlib.nullcontext(str(isolated_home))

    with runtime_ctx as runtime_value:
        runtime_root = Path(runtime_value)
        config_dir = runtime_root / ".opencode"
        config_dir.mkdir(parents=True, exist_ok=True)
        xdg_root = runtime_root / ".stream-eval-opencode"
        xdg_config = xdg_root / "config"
        xdg_data = xdg_root / "data"
        xdg_state = xdg_root / "state"
        xdg_cache = xdg_root / "cache"
        for directory in (xdg_config, xdg_data, xdg_state, xdg_cache):
            directory.mkdir(parents=True, exist_ok=True)

        source_data = Path(
            env.get("XDG_DATA_HOME") or real_home / ".local" / "share"
        )
        source_auth = source_data / "opencode" / "auth.json"
        if source_auth.is_file():
            auth_dir = xdg_data / "opencode"
            auth_dir.mkdir(parents=True, exist_ok=True)
            os.symlink(source_auth, auth_dir / "auth.json")

        skills_paths = []
        if profile == "restricted":
            source_config = Path(
                env.get("XDG_CONFIG_HOME") or real_home / ".config"
            )
            candidates = (
                real_home / ".agents" / "skills",
                real_home / ".claude" / "skills",
                source_config / "opencode" / "skill",
                source_config / "opencode" / "skills",
            )
            skills_paths = [str(path) for path in candidates if path.is_dir()]

        config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {},
            "permission": {
                "task": "deny",
                "skill": {
                    "*": "allow",
                    "customize-opencode": "deny",
                },
            },
        }
        if skills_paths:
            config["skills"] = {"paths": skills_paths}

        clean.update({
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_DATA_HOME": str(xdg_data),
            "XDG_STATE_HOME": str(xdg_state),
            "XDG_CACHE_HOME": str(xdg_cache),
            "OPENCODE_CONFIG_DIR": str(config_dir),
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "OPENCODE_DB": str(xdg_data / "opencode" / "stream-eval.db"),
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_PRUNE": "1",
            "OPENCODE_PURE": "1",
        })
        yield clean

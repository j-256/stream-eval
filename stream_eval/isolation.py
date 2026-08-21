"""Hermetic skill isolation for stream-eval

Each agent spawn runs against a temp HOME containing only the skill under
test and any explicit siblings supplied through --also-install.

Public surface:
- parse_skill_md_name(skill_path): read the canonical name from
  <skill_path>/SKILL.md frontmatter.
- prepare_isolated_home(agent, skill_path, also_install): context manager that
  yields (home_dir, skill_name) and cleans up the temp HOME on exit
  (success or exception).
"""
import atexit
import contextlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from stream_eval.agents import DEFAULT_AGENT, SUPPORTED_AGENTS


# Isolated spawns get a throwaway HOME (rmtree'd per run) and the DSC scrape
# cache is $HOME-derived, so by default every isolated run scrapes cold and
# shares nothing -- defeating the cache TTL and any pre-warm, and hammering
# developer.salesforce.com under parallel workers. Point each isolated run's
# ~/.cache/dsc-scrape at ONE persistent shared dir so warming compounds across
# runs. Kept OUTSIDE the user's HOME (under the temp dir) so the isolated HOME
# still shares nothing with the real ~ -- this is the harness's own dedicated
# cache, never the operator's interactive ~/.cache/dsc-scrape. Survives across
# runs (a stable name, not per-pid) so the reaper leaves it; the OS clears it
# eventually, which is fine -- a cold session just re-warms serially
ISOLATED_CACHE_DIR = os.path.join(
    tempfile.gettempdir(), "stream-eval-cache", "dsc-scrape"
)


class SkillMetadataError(Exception):
    """Raised when a skill path's SKILL.md is missing, malformed, or
    lacks the required `name:` field."""


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)",
    re.DOTALL,
)


def parse_skill_md_name(skill_path):
    """Return the canonical skill name from <skill_path>/SKILL.md.

    Raises SkillMetadataError when:
    - SKILL.md is missing
    - the file has no YAML frontmatter (no leading `---` block)
    - the frontmatter has no `name:` key
    """
    skill_path = Path(skill_path)
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        raise SkillMetadataError(
            f"SKILL.md not found in {skill_path!s}"
        )
    text = skill_md.read_text()
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillMetadataError(
            f"{skill_md!s} has no YAML frontmatter (expected leading --- block)"
        )
    body = m.group(1)
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("name:"):
            value = line.split(":", 1)[1].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if not value:
                raise SkillMetadataError(
                    f"{skill_md!s} has empty `name:` field"
                )
            return value
    raise SkillMetadataError(
        f"{skill_md!s} frontmatter has no `name:` field"
    )


# Minimal Claude settings stub for the temp HOME
_SETTINGS_STUB = {
    "mcpServers": {},
}


@contextlib.contextmanager
def prepare_isolated_home(*, skill_path, also_install=(), agent=DEFAULT_AGENT):
    """Context manager: build a temp HOME containing only the skill at
    `skill_path` (and any siblings in `also_install`), yield (home_dir,
    skill_name), and clean up on exit (success or exception).

    Claude installs under <home>/.claude/skills and receives a minimal
    settings.json. Codex installs under <home>/.agents/skills. OpenCode
    installs under <home>/.opencode/skills. All layouts use symlinks to the
    supplied skill directories.

    `home_dir` is a string suitable for assignment to a child process's
    HOME env var. `skill_name` is read from <skill_path>/SKILL.md.

    Raises SkillMetadataError if the skill_path doesn't have a parseable
    SKILL.md.
    """
    if agent not in SUPPORTED_AGENTS:
        raise ValueError(f"unsupported isolation agent {agent!r}")

    skill_path = Path(skill_path).resolve()
    skill_name = parse_skill_md_name(skill_path)

    sibling_paths = []
    seen_names = {skill_name}
    for sib in also_install:
        sib_path = Path(sib).resolve()
        sib_name = parse_skill_md_name(sib_path)
        if sib_name in seen_names:
            raise SkillMetadataError(
                f"duplicate skill name {sib_name!r}: --also-install path "
                f"{sib_path!s} has the same name as a previously-resolved "
                f"skill (primary or earlier sibling)"
            )
        seen_names.add(sib_name)
        sibling_paths.append((sib_path, sib_name))

    tmp = tempfile.mkdtemp(prefix=f"stream-eval-{os.getpid()}-")
    try:
        agent_dirs = {
            "claude": ".claude",
            "codex": ".agents",
            "opencode": ".opencode",
        }
        agent_dir = agent_dirs[agent]
        skills_dir = Path(tmp) / agent_dir / "skills"
        skills_dir.mkdir(parents=True)
        os.symlink(skill_path, skills_dir / skill_name)
        for sib_path, sib_name in sibling_paths:
            os.symlink(sib_path, skills_dir / sib_name)
        if agent == "claude":
            settings = Path(tmp) / ".claude" / "settings.json"
            settings.write_text(json.dumps(_SETTINGS_STUB, indent=2))
        # Share one persistent DSC scrape cache across all isolated spawns
        # (ISOLATED_CACHE_DIR). The symlink lives inside the throwaway HOME, so
        # the rmtree below only unlinks it -- the shared cache and its TTL survive
        os.makedirs(ISOLATED_CACHE_DIR, exist_ok=True)
        cache_dir = Path(tmp) / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.symlink(ISOLATED_CACHE_DIR, cache_dir / "dsc-scrape")
        yield (tmp, skill_name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_TMP_PREFIX = "stream-eval-"


def _reap_orphaned_temp_dirs():
    """Remove stream-eval-<pid>-* directories belonging to the CURRENT
    pid. Called via atexit on harness shutdown to clean up temp HOMEs
    that escaped their context manager (caller bug or hard crash).

    We deliberately limit to the current pid: cross-pid cleanup risks
    deleting another live harness's state. Hard kill (SIGKILL) leaves
    litter; that's an accepted tradeoff -- a paranoid sweep on startup
    could be added later if the litter accumulates in practice.
    """
    pid = os.getpid()
    needle = f"{_TMP_PREFIX}{pid}-"
    tmpdir = Path(tempfile.gettempdir())
    if not tmpdir.is_dir():
        return
    for entry in tmpdir.iterdir():
        if entry.name.startswith(needle) and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


atexit.register(_reap_orphaned_temp_dirs)

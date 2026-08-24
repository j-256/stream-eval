"""Tests for stream_eval.isolation: SKILL.md frontmatter parsing,
prepare_isolated_home temp-HOME setup, and the cleanup invariants."""
import json
import os
import tempfile
from pathlib import Path

import pytest

from stream_eval.isolation import (
    ISOLATED_CACHE_DIR,
    SkillMetadataError,
    parse_skill_md_name,
    prepare_isolated_home,
)


def _write_skill_md(dir_path: Path, body: str) -> Path:
    p = dir_path / "SKILL.md"
    p.write_text(body)
    return p


def test_parse_skill_md_name_reads_frontmatter_name(tmp_path):
    _write_skill_md(
        tmp_path,
        "---\n"
        "name: my-skill\n"
        "description: does a thing\n"
        "---\n"
        "\n"
        "# Body of the skill\n",
    )
    assert parse_skill_md_name(tmp_path) == "my-skill"


def test_parse_skill_md_name_strips_quotes(tmp_path):
    _write_skill_md(
        tmp_path,
        "---\n"
        'name: "my-skill"\n'
        "description: does a thing\n"
        "---\n",
    )
    assert parse_skill_md_name(tmp_path) == "my-skill"


def test_parse_skill_md_name_missing_file_raises(tmp_path):
    with pytest.raises(SkillMetadataError, match="SKILL.md not found"):
        parse_skill_md_name(tmp_path)


def test_parse_skill_md_name_missing_frontmatter_raises(tmp_path):
    _write_skill_md(tmp_path, "# Just a body, no frontmatter\n")
    with pytest.raises(SkillMetadataError, match="frontmatter"):
        parse_skill_md_name(tmp_path)


def test_parse_skill_md_name_missing_name_field_raises(tmp_path):
    _write_skill_md(
        tmp_path,
        "---\n"
        "description: does a thing\n"
        "---\n",
    )
    with pytest.raises(SkillMetadataError, match="name"):
        parse_skill_md_name(tmp_path)


def _make_skill(dir_path: Path, name: str) -> Path:
    skill_dir = dir_path / name
    skill_dir.mkdir()
    _write_skill_md(
        skill_dir,
        f"---\nname: {name}\ndescription: test\n---\n",
    )
    return skill_dir


def test_prepare_isolated_home_creates_skills_symlink(tmp_path):
    skill = _make_skill(tmp_path, "skill-a")

    with prepare_isolated_home(skill_path=skill, also_install=()) as (home, name):
        assert name == "skill-a"
        skills_dir = Path(home) / ".claude" / "skills"
        assert skills_dir.is_dir()
        installed = skills_dir / "skill-a"
        assert installed.is_symlink()
        assert installed.resolve() == skill.resolve()


def test_prepare_isolated_home_installs_siblings(tmp_path):
    skill = _make_skill(tmp_path, "skill-a")
    sibling = _make_skill(tmp_path, "skill-b")

    with prepare_isolated_home(
        skill_path=skill, also_install=(sibling,)
    ) as (home, _name):
        skills_dir = Path(home) / ".claude" / "skills"
        assert (skills_dir / "skill-a").is_symlink()
        assert (skills_dir / "skill-b").is_symlink()


def test_prepare_isolated_codex_home_uses_agents_skills(tmp_path):
    skill = _make_skill(tmp_path, "skill-a")

    with prepare_isolated_home(
        skill_path=skill,
        also_install=(),
        agent="codex",
    ) as (home, name):
        assert name == "skill-a"
        installed = Path(home) / ".agents" / "skills" / "skill-a"
        assert installed.is_symlink()
        assert installed.resolve() == skill.resolve()
        assert not (Path(home) / ".claude").exists()


def test_prepare_isolated_opencode_home_uses_opencode_skills(tmp_path):
    skill = _make_skill(tmp_path, "skill-a")

    with prepare_isolated_home(
        skill_path=skill,
        also_install=(),
        agent="opencode",
    ) as (home, name):
        assert name == "skill-a"
        installed = Path(home) / ".opencode" / "skills" / "skill-a"
        assert installed.is_symlink()
        assert installed.resolve() == skill.resolve()
        assert not (Path(home) / ".claude").exists()
        assert not (Path(home) / ".agents").exists()


def test_prepare_isolated_home_rejects_unknown_agent(tmp_path):
    skill = _make_skill(tmp_path, "skill-a")

    with pytest.raises(ValueError, match="unsupported isolation agent"):
        with prepare_isolated_home(
            skill_path=skill,
            also_install=(),
            agent="other",
        ):
            pass


def test_prepare_isolated_home_writes_settings_stub(tmp_path):
    """The stub explicitly disables MCP servers so the spawn doesn't
    inherit anything from a system-wide /etc/claude/settings.json or
    similar. The CLI flags `--strict-mcp-config` and
    `--disallowedTools Agent` belt-and-suspender this, but the stub is
    what guarantees the temp HOME itself is empty of MCP config."""
    skill = _make_skill(tmp_path, "skill-a")

    with prepare_isolated_home(skill_path=skill, also_install=()) as (home, _name):
        settings = Path(home) / ".claude" / "settings.json"
        assert settings.is_file()
        data = json.loads(settings.read_text())
        assert isinstance(data, dict)
        # mcpServers MUST be present and empty; a missing key would let
        # the spawn fall back to system defaults.
        assert "mcpServers" in data
        assert data["mcpServers"] == {}


def test_prepare_isolated_home_shares_dsc_cache_across_runs(tmp_path, monkeypatch):
    """The isolated HOME's .cache/dsc-scrape is a symlink to the shared
    ISOLATED_CACHE_DIR, which SURVIVES cleanup: rmtree unlinks the symlink but
    the shared cache (and anything written through it) persists, so warming
    compounds across runs."""
    # the real default lives OUTSIDE the user HOME (this is what keeps the
    # hermetic-isolation guarantee intact; mirrors the no-symlink-into-~ test)
    real_home = os.path.realpath(os.path.expanduser("~"))
    assert not os.path.realpath(ISOLATED_CACHE_DIR).startswith(real_home + os.sep)
    # redirect to tmp so the test never touches the real shared cache
    shared = tmp_path / "shared-cache" / "dsc-scrape"
    monkeypatch.setattr("stream_eval.isolation.ISOLATED_CACHE_DIR", str(shared))
    skill = _make_skill(tmp_path, "skill-a")
    with prepare_isolated_home(skill_path=skill, also_install=()) as (home, _name):
        link = Path(home) / ".cache" / "dsc-scrape"
        assert link.is_symlink()
        assert os.path.realpath(str(link)) == os.path.realpath(str(shared))
        (link / "warm.json").write_text("{}")  # write through the symlink
    assert not Path(home).exists(), "temp HOME cleaned up"
    assert shared.is_dir(), "shared cache survived rmtree"
    assert (shared / "warm.json").read_text() == "{}", "warmed contents survived"


def test_prepare_isolated_home_cleans_up_on_exit(tmp_path):
    skill = _make_skill(tmp_path, "skill-a")

    with prepare_isolated_home(skill_path=skill, also_install=()) as (home, _name):
        captured_home = Path(home)
        assert captured_home.exists()
    assert not captured_home.exists()


def test_prepare_isolated_home_cleans_up_on_exception(tmp_path):
    skill = _make_skill(tmp_path, "skill-a")

    captured_home = None
    with pytest.raises(RuntimeError, match="boom"):
        with prepare_isolated_home(skill_path=skill, also_install=()) as (home, _n):
            captured_home = Path(home)
            assert captured_home.exists()
            raise RuntimeError("boom")
    assert not captured_home.exists()


def test_isolated_home_excludes_user_globally_installed_skills(tmp_path):
    """Under prepare_isolated_home, the temp HOME's .claude/skills/ must
    NOT contain anything from the user's real ~/.claude/skills/."""
    skill = _make_skill(tmp_path, "skill-a")

    with prepare_isolated_home(skill_path=skill, also_install=()) as (home, _name):
        skills_dir = Path(home) / ".claude" / "skills"
        installed = sorted(p.name for p in skills_dir.iterdir())
        # Only the skill under test should be present.
        assert installed == ["skill-a"]


def test_isolated_home_does_not_share_filesystem_with_user_home(tmp_path):
    """The temp HOME must be a separate directory from the user's real
    HOME -- writes to one mustn't be visible from the other."""
    skill = _make_skill(tmp_path, "skill-a")
    real_home = os.path.expanduser("~")

    with prepare_isolated_home(skill_path=skill, also_install=()) as (home, _name):
        assert os.path.realpath(home) != os.path.realpath(real_home)
        # And no symlink to ~ either:
        for entry in Path(home).rglob("*"):
            if entry.is_symlink():
                target = os.path.realpath(entry)
                assert not target.startswith(real_home + os.sep), \
                    f"unexpected symlink into user HOME: {entry} -> {target}"


def test_atexit_reaper_removes_orphaned_dirs(tmp_path, monkeypatch):
    """If a stream-eval-<pid>-<random> dir from THIS pid is left behind
    (test simulates crash mid-spawn), the reaper should clean it up.

    We don't actually run atexit; we call the reaper directly.
    """
    from stream_eval.isolation import _reap_orphaned_temp_dirs

    # Simulate a crashed-mid-spawn temp dir for THIS pid.
    pid = os.getpid()
    orphan = tmp_path / f"stream-eval-{pid}-orphan-abc"
    orphan.mkdir()
    assert orphan.exists()

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _reap_orphaned_temp_dirs()

    assert not orphan.exists()


def test_atexit_reaper_leaves_other_pids_alone(tmp_path, monkeypatch):
    """A temp dir from a DIFFERENT (live) pid must not be reaped."""
    from stream_eval.isolation import _reap_orphaned_temp_dirs

    other_pid = 1  # init -- guaranteed live on Unix
    other = tmp_path / f"stream-eval-{other_pid}-other-xyz"
    other.mkdir()

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _reap_orphaned_temp_dirs()

    assert other.exists()


def test_prepare_isolated_home_rejects_sibling_with_primary_name(tmp_path):
    """Two skills with the same name in the same skills dir would yield a
    cryptic FileExistsError from os.symlink. Catch it earlier with a
    domain-specific SkillMetadataError pointing at --also-install."""
    skill = _make_skill(tmp_path, "skill-a")
    # Build a sibling at a different path but with the same `name:` field.
    sibling_dir = tmp_path / "elsewhere"
    sibling_dir.mkdir()
    sibling = _make_skill(sibling_dir, "skill-a")

    with pytest.raises(SkillMetadataError, match="duplicate skill name"):
        with prepare_isolated_home(
            skill_path=skill, also_install=(sibling,)
        ):
            pass


def test_prepare_isolated_home_rejects_two_siblings_with_same_name(tmp_path):
    """Two --also-install paths whose SKILL.md frontmatter both name
    'skill-x' should raise as cleanly as a primary/sibling collision."""
    skill = _make_skill(tmp_path, "skill-a")
    sib1_dir = tmp_path / "sib1"
    sib1_dir.mkdir()
    sib1 = _make_skill(sib1_dir, "shared-name")
    sib2_dir = tmp_path / "sib2"
    sib2_dir.mkdir()
    sib2 = _make_skill(sib2_dir, "shared-name")

    with pytest.raises(SkillMetadataError, match="duplicate skill name"):
        with prepare_isolated_home(
            skill_path=skill, also_install=(sib1, sib2)
        ):
            pass


def test_parse_skill_md_name_strips_single_quotes(tmp_path):
    """Single-quoted name values should also strip cleanly. The regex
    accepts both quote styles."""
    _write_skill_md(
        tmp_path,
        "---\n"
        "name: 'my-skill'\n"
        "description: does a thing\n"
        "---\n",
    )
    assert parse_skill_md_name(tmp_path) == "my-skill"


def test_env_overlay_reaches_child_process(tmp_path):
    """End-to-end check that the env dict passed to
    run_with_retry_aware_bail propagates HOME to the spawned child.
    Uses /usr/bin/env (no agent CLI needed) so it runs in CI."""
    from stream_eval.subprocess import run_with_retry_aware_bail

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    transcript = tmp_path / "out.jsonl"

    # /usr/bin/env prints all env vars, one per line. We use it as a
    # Poor-man's agent CLI for env propagation testing
    bail = run_with_retry_aware_bail(
        cmd=["/usr/bin/env"],
        stdout_path=str(transcript),
        env={"HOME": str(fake_home), "PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        timeout=10,
    )
    assert bail["exit_code"] == 0
    output = transcript.read_text()
    assert f"HOME={fake_home}" in output

#!/usr/bin/env python3
"""Synthesis-eval harness for agent skills

Drives the selected agent adapter against fixture queries, normalizes its
JSONL transcript, and evaluates typed assertions.

"""
import argparse
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from stream_eval.agents import (
    DEFAULT_AGENT,
    SUPPORTED_AGENTS,
    SUPPORTED_PROFILES,
)
from stream_eval.control import install_signal_handlers, serve_socket
from stream_eval.runner import run_eval
from stream_eval.transcript import (
    ParsedTranscript,
    ToolUse,
    parse_transcript as parse_agent_transcript,
)


KIND_REQUIRED_FIELDS = {
    "final_text_matches": ["pattern"],
    "final_text_excludes": ["pattern"],
    "tool_input_matches": ["tool", "field", "pattern"],
    "tool_sequence_includes": ["pattern"],
    "action_input_matches": ["action", "field", "pattern"],
    "action_sequence_includes": ["pattern"],
    "artifact_content_matches": ["pattern"],
}


class FixtureSchemaError(Exception):
    pass

@dataclass
class AssertionResult:
    kind: str
    args: dict
    pass_: bool
    message: str
    because: str


def validate_fixtures(fixtures):
    if not isinstance(fixtures, list):
        raise FixtureSchemaError("top-level value must be a list of fixtures")
    seen_names = set()
    for i, fx in enumerate(fixtures):
        prefix = f"fixture[{i}]"
        if not isinstance(fx, dict):
            raise FixtureSchemaError(f"{prefix} must be an object")
        name = fx.get("name")
        if not isinstance(name, str) or not name:
            raise FixtureSchemaError(f"{prefix} missing required string 'name'")
        if name in seen_names:
            raise FixtureSchemaError(f"{prefix} duplicate name {name!r}")
        seen_names.add(name)
        if not isinstance(fx.get("query"), str) or not fx["query"]:
            raise FixtureSchemaError(f"{prefix} ({name}) missing required string 'query'")
        assertions = fx.get("assertions", [])
        if not isinstance(assertions, list):
            raise FixtureSchemaError(f"{prefix} ({name}) 'assertions' must be a list")
        for j, a in enumerate(assertions):
            apref = f"{prefix} ({name}).assertions[{j}]"
            if not isinstance(a, dict):
                raise FixtureSchemaError(f"{apref} must be an object")
            kind = a.get("kind")
            if kind not in KIND_REQUIRED_FIELDS:
                raise FixtureSchemaError(
                    f"{apref} unknown kind {kind!r}; must be one of {sorted(KIND_REQUIRED_FIELDS)}"
                )
            for required in KIND_REQUIRED_FIELDS[kind]:
                if required not in a:
                    raise FixtureSchemaError(
                        f"{apref} kind={kind} missing required field {required!r}"
                    )


def evaluate_assertion(assertion, parsed):
    kind = assertion.get("kind")
    because = assertion.get("because", "")
    args = {k: v for k, v in assertion.items() if k not in ("kind", "because")}

    if kind == "final_text_matches":
        pattern = assertion["pattern"]
        if parsed.final_text is None:
            return AssertionResult(kind, args, False,
                                   "no final answer recorded", because)
        if re.search(pattern, parsed.final_text):
            return AssertionResult(kind, args, True,
                                   "matched", because)
        return AssertionResult(kind, args, False,
                               f"pattern {pattern!r} not found", because)

    if kind == "final_text_excludes":
        pattern = assertion["pattern"]
        if parsed.final_text is None:
            return AssertionResult(kind, args, False,
                                   "no final answer recorded", because)
        if re.search(pattern, parsed.final_text):
            return AssertionResult(kind, args, False,
                                   f"pattern {pattern!r} unexpectedly matched",
                                   because)
        return AssertionResult(kind, args, True, "no match (good)", because)

    if kind == "tool_input_matches":
        tool = assertion["tool"]
        field = assertion["field"]
        pattern = assertion["pattern"]
        for tu in parsed.tool_uses:
            if tu.name != tool:
                continue
            value = tu.input.get(field, "")
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            if re.search(pattern, str(value)):
                return AssertionResult(kind, args, True,
                                       f"matched on {tool}.{field}", because)
        return AssertionResult(kind, args, False,
                               f"no {tool} call had {field} matching {pattern!r}",
                               because)

    if kind == "tool_sequence_includes":
        pattern = assertion["pattern"]
        sequence = "\n".join(tu.name for tu in parsed.tool_uses)
        if re.search(pattern, sequence):
            return AssertionResult(kind, args, True,
                                   "sequence matched", because)
        return AssertionResult(kind, args, False,
                               f"sequence {sequence!r} did not match {pattern!r}",
                               because)

    if kind == "action_input_matches":
        action_name = assertion["action"]
        field_name = assertion["field"]
        pattern = assertion["pattern"]
        for action in parsed.actions:
            if action.name != action_name:
                continue
            value = action.input.get(field_name, "")
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            if re.search(pattern, str(value)):
                return AssertionResult(
                    kind, args, True,
                    f"matched on {action_name}.{field_name}", because,
                )
        return AssertionResult(
            kind, args, False,
            f"no {action_name} action had {field_name} matching {pattern!r}",
            because,
        )

    if kind == "action_sequence_includes":
        pattern = assertion["pattern"]
        sequence = "\n".join(action.name for action in parsed.actions)
        if re.search(pattern, sequence):
            return AssertionResult(kind, args, True, "sequence matched", because)
        return AssertionResult(
            kind, args, False,
            f"sequence {sequence!r} did not match {pattern!r}", because,
        )

    if kind == "artifact_content_matches":
        pattern = assertion["pattern"]
        path_pattern = assertion.get("path")
        for artifact in parsed.artifacts:
            if path_pattern and not re.search(path_pattern, artifact.path):
                continue
            if re.search(pattern, artifact.content):
                return AssertionResult(
                    kind, args, True,
                    f"matched artifact {artifact.path}", because,
                )
        scope = f" with path matching {path_pattern!r}" if path_pattern else ""
        return AssertionResult(
            kind, args, False,
            f"no artifact{scope} had content matching {pattern!r}", because,
        )

    raise ValueError(f"unknown assertion kind: {kind!r}")


def transcript_dir_for(out_path):
    """Per-run transcript directory, namespaced by `--out` stem so
    multi-phase iterations sharing one results dir don't clobber each
    other's JSONLs."""
    return out_path.parent / "transcripts" / out_path.stem


def parse_transcript(
    path, *, agent=DEFAULT_AGENT, artifacts=(), known_skills=(),
):
    """Compatibility wrapper around the portable transcript parser"""
    return parse_agent_transcript(
        path,
        agent=agent,
        artifacts=artifacts,
        known_skills=known_skills,
    )


def get_synthesis_query(fixture):
    """Module-level (picklable) query extractor. ProcessPoolExecutor on
    macOS spawn-mode workers unpickle this; lambdas wouldn't survive."""
    return fixture["query"]


def score_synthesis_run(fixture, transcript_path, bail):
    """Synthesis scoring callback for stream_eval.runner.run_eval.

    Receives the fixture, the path to the (already-written) transcript,
    and the bail dict. Returns (pass: bool, kind_extra: dict).

    Called on every run including timed-out ones -- a timeout tells us
    about runtime, not about whether assertions hold against whatever
    transcript the partial run produced. The runner forces pass_=False
    on timeouts regardless; running here on timeouts preserves
    first_tool / first_skill / assertion_results in kind_extra. Partial
    transcripts may have no final result event, so assertions like
    final_text_matches will simply fail to non-match rather than error.

    kind_extra carries first_tool + first_skill so the runner can
    surface them on the canonical stderr line; assertion_results +
    expected_skill_pass are downstream consumers (results.json).
    """
    expected_skill = fixture.get("expected_skill")
    known_skills = (expected_skill,) if expected_skill else ()
    parsed = parse_transcript(
        transcript_path,
        agent=bail.get("agent", DEFAULT_AGENT),
        artifacts=bail.get("artifacts", ()),
        known_skills=known_skills,
    )

    first_tool = None
    if parsed.tool_uses:
        first_tool = parsed.tool_uses[0].name

    first_action = parsed.first_action.name if parsed.first_action else None
    first_skill = parsed.first_skill
    expected_skill_pass = (
        expected_skill is None or first_skill == expected_skill
    )

    assertion_records = []
    for a in fixture.get("assertions", []):
        r = evaluate_assertion(a, parsed)
        assertion_records.append({
            "kind": r.kind,
            "args": r.args,
            "pass": r.pass_,
            "message": r.message,
            "because": r.because,
        })

    all_pass = expected_skill_pass and all(
        r["pass"] for r in assertion_records
    )
    return all_pass, {
        "first_tool": first_tool,
        "first_action": first_action,
        "first_skill": first_skill,
        "expected_skill_pass": expected_skill_pass,
        "assertion_results": assertion_records,
    }


def build_argument_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("-e", "--eval", required=True,
                    help="Path to synthesis-eval.json")
    ap.add_argument("-o", "--out", required=True,
                    help="Path to write results JSON")
    ap.add_argument("-r", "--runs", type=int, default=5,
                    help="Runs per fixture (default 5)")
    ap.add_argument("-l", "--lenient", action="store_true",
                    help="Pass if majority of runs pass (default: strict -- "
                         "all runs must pass)")
    ap.add_argument("-w", "--workers", type=int, default=4)
    ap.add_argument("-t", "--timeout", type=int, default=300,
                    help="Per-run wall-clock backstop in seconds "
                         "(default 300). Measures effective model-thinking "
                         "time; adapter-reported retry-backoff windows are "
                         "excluded.")
    ap.add_argument("-c", "--cwd", default=None)
    ap.add_argument(
        "-a", "--agent",
        choices=SUPPORTED_AGENTS,
        default=os.environ.get("STREAM_EVAL_AGENT", DEFAULT_AGENT),
        help=(
            "Agent CLI backend (default: STREAM_EVAL_AGENT or "
            f"{DEFAULT_AGENT})"
        ),
    )
    ap.add_argument(
        "-p", "--profile", choices=SUPPORTED_PROFILES,
        default=os.environ.get("STREAM_EVAL_PROFILE", "isolated"),
        help="Isolation profile for the spawned agent. 'isolated' "
             "(default) uses a temp HOME with only the skill under test; "
             "'restricted' uses the user's real HOME but strips MCP and "
             "subagents; "
             "'inherit' runs with the user's full environment.",
    )
    ap.add_argument("-s", "--skill-path", required=False,
                    help="Path to the skill directory (containing SKILL.md). "
                         "Required for the default 'isolated' profile.")
    ap.add_argument("-i", "--also-install", action="append", default=[],
                    metavar="PATH",
                    help="Path to a sibling skill to install alongside the "
                         "skill under test. May be repeated.")
    ap.add_argument("--skill-name", required=False, default=None,
                    help="Override the skill name. Default: read from "
                         "SKILL.md frontmatter when --skill-path is given, "
                         "else from the --eval JSON's parent directory name.")
    return ap


def main(argv=None):
    ap = build_argument_parser()
    args = ap.parse_args(argv)

    from stream_eval.isolation import parse_skill_md_name

    # Pre-flight: profile=isolated needs a skill_path so prepare_isolated_home
    # has something to symlink. Fail at the CLI rather than in every worker.
    if args.profile == "isolated" and not args.skill_path:
        ap.error(
            "--skill-path is required when --profile=isolated "
            "(the default). Pass --profile=inherit (or =restricted) to "
            "test the user's globally-installed skill instead."
        )

    if args.skill_path:
        skill_path = os.path.abspath(args.skill_path)
        skill_name = args.skill_name or parse_skill_md_name(skill_path)
    else:
        skill_path = None
        skill_name = args.skill_name or Path(args.eval).resolve().parent.name
    also_install = [os.path.abspath(p) for p in args.also_install]

    os.environ["STREAM_EVAL_PROFILE"] = args.profile

    install_signal_handlers()
    sock_path = f"/tmp/stream-eval-{os.getpid()}.sock"
    threading.Thread(
        target=serve_socket, args=(sock_path,), daemon=True
    ).start()

    try:
        return _run_with_socket(args, skill_name, skill_path, also_install)
    finally:
        # Always remove the socket file, even on early-return paths
        # (e.g., FixtureSchemaError). The daemon serve_socket thread is
        # killed at process exit without running its own finally block.
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


def _run_with_socket(args, skill_name, skill_path, also_install):
    """The body of main() after socket setup. Pulled out so main()'s
    `finally` cleanup of the socket file is structurally obvious.
    Returns the exit code."""
    cwd = args.cwd or os.getcwd()

    with open(args.eval) as f:
        fixtures = json.load(f)
    try:
        validate_fixtures(fixtures)
    except FixtureSchemaError as e:
        print(f"FIXTURE SCHEMA ERROR: {e}", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_dir = transcript_dir_for(out_path)

    def summarize(fixtures_with_runs):
        summary = []
        for entry in fixtures_with_runs:
            fx = entry["fixture"]
            runs = sorted(entry["runs"], key=lambda r: r["run_idx"])
            run_passes = [r["pass_"] for r in runs]
            triggers = sum(
                1 for r in runs
                if r["kind_extra"].get("expected_skill_pass")
            )
            if args.lenient:
                fx_pass = (
                    sum(run_passes) / len(run_passes) >= 0.5
                ) if run_passes else False
            else:
                fx_pass = (
                    all(run_passes) and len(run_passes) == args.runs
                )
            summary.append({
                "name": fx["name"],
                "query": fx["query"],
                "expected_skill": fx.get("expected_skill"),
                "hypothesis": fx.get("hypothesis", ""),
                "pass": fx_pass,
                "triggers": triggers,
                "runs": runs,
            })
        return summary

    results, exit_code = run_eval(
        kind="synthesis",
        fixtures=fixtures,
        get_fixture_id=lambda fx: fx["name"],
        get_query=get_synthesis_query,
        score_run=score_synthesis_run,
        summarize=summarize,
        runs_per_fixture=args.runs,
        workers=args.workers,
        timeout=args.timeout,
        cwd=cwd,
        transcript_dir=transcript_dir,
        summary_label="fixtures",
        skill_name=skill_name,
        eval_path=args.eval,
        skill_path=skill_path,
        also_install=also_install,
        agent=args.agent,
        allow_worktree_writes=True,
    )

    results["strict"] = not args.lenient
    results["fixtures_passed"] = sum(
        1 for r in results["results"] if r["pass"]
    )
    results["fixtures_failed"] = (
        len(results["results"]) - results["fixtures_passed"]
    )

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

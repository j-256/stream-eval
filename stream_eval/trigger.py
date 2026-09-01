#!/usr/bin/env python3
"""Trigger-accuracy eval harness for skills

Fires the selected agent and scores its first normalized action. A run counts
as triggered when that action activates the target skill. Each adapter owns
the host-specific activation signal.

A query passes when its trigger rate meets its `should_trigger` expectation
with a 0.5 threshold.

Bail signal is api_retry-aware. The CLI emits stream-json events of the
shape `{"type":"system","subtype":"api_retry","attempt":N,"max_retries":M,
"error":"rate_limit"|"server_error",...}` while waiting on the upstream
API. The harness streams the JSONL live and uses the most-recent retry
event as a separate bail signal: a run aborts when the CLI's full retry
budget is exhausted (attempt == max_retries on the most recent retry
event), which is the documented "retry budget poisoned" condition.

The wall clock (--timeout) measures effective model-thinking time --
retry-backoff windows are excluded from the deadline so heavy throttle
doesn't pre-empt the api_retry-exhaustion signal. A separate absolute
backstop at 4 * timeout catches the rare stuck-during-retry case where
the CLI is wedged inside a retry sleep and emits nothing further; that
surfaces as `timeout_reason=wall_clock_in_retry` and is distinct from
the regular `wall_clock` (model-genuinely-slow) signal.

Exit codes match stream_eval.synthesis:
  0 -- all queries pass
  1 -- at least one query fails
  2 -- fixture schema error (returned before any runs spawn)
  3 -- aborted on retry-budget exhaustion or absolute wall clock (no
       results.json written; throttle-corrupted partial data was the
       exact misleading state the abort is preventing -- re-run when the
       upstream API has recovered)

Usage:
  stream-eval trigger \\
    --eval evals/dsc-endpoint-help/trigger-eval.json \\
    --skill-name dsc-endpoint-help \\
    --runs 3 --workers 4 --timeout 300 \\
    --out evals/dsc-endpoint-help/runs/iteration-N/results.json
"""
import argparse
import functools
import json
import os
import sys
import threading
from pathlib import Path

from stream_eval.agents import (
    DEFAULT_AGENT,
    SUPPORTED_AGENTS,
    SUPPORTED_PROFILES,
)
from stream_eval.control import install_signal_handlers, serve_socket
from stream_eval.runner import run_eval
from stream_eval.transcript import parse_transcript


class FixtureSchemaError(Exception):
    """Raised when an eval JSON file fails schema validation. Mirrors
    stream_eval.synthesis.FixtureSchemaError so the two harnesses share
    exit-code 2 semantics. Each harness defines its own class to keep
    them decoupled (the runner doesn't know about either)."""


def validate_fixtures(fixtures):
    """Validate a trigger eval set: top-level list of dicts, each with a
    non-empty `query` (string) and a `should_trigger` (bool); optional
    `name` (string) which must be unique across the set if present.
    Raises FixtureSchemaError on the first violation, with a message
    that names the offending index so authors can locate it."""
    if not isinstance(fixtures, list):
        raise FixtureSchemaError("top-level value must be a list of fixtures")
    seen_names = set()
    for i, fx in enumerate(fixtures):
        prefix = f"fixture[{i}]"
        if not isinstance(fx, dict):
            raise FixtureSchemaError(f"{prefix} must be an object")
        if not isinstance(fx.get("query"), str) or not fx["query"]:
            raise FixtureSchemaError(
                f"{prefix} missing required non-empty string 'query'"
            )
        if not isinstance(fx.get("should_trigger"), bool):
            raise FixtureSchemaError(
                f"{prefix} missing required bool 'should_trigger'"
            )
        name = fx.get("name")
        if name is not None:
            if not isinstance(name, str) or not name:
                raise FixtureSchemaError(
                    f"{prefix} 'name' must be a non-empty string when present"
                )
            if name in seen_names:
                raise FixtureSchemaError(
                    f"{prefix} duplicate name {name!r}"
                )
            seen_names.add(name)


def get_trigger_query(fixture):
    """Module-level query extractor (rather than an inline lambda) so
    it survives ProcessPoolExecutor pickling on macOS spawn-mode workers.
    Mirrors get_synthesis_query in stream_eval.synthesis."""
    return fixture["query"]


def transcript_dir_for(out_path):
    """Per-run transcript directory, namespaced by `--out` stem so
    multi-phase iterations sharing one results dir don't clobber each
    other's JSONLs. Mirrors transcript_dir_for in stream_eval.synthesis."""
    return out_path.parent / "transcripts" / out_path.stem


def score_trigger_run(fixture, transcript_path, bail, *, target_skill):
    """Trigger-eval scoring callback for stream_eval.runner.run_eval.

    Receives the fixture, the path to the (already-written) transcript,
    and the bail dict. Called on every run including timed-out ones --
    a timeout tells us about runtime, not whether the skill triggered.
    The runner forces pass_=False on timeouts regardless of what we
    return; the value of running here on timeouts is preserving
    first_tool / first_skill in kind_extra so partial-run transcripts
    don't lose their trigger signal.

    Returns (pass: bool, kind_extra: dict). pass=True iff the run's
    trigger outcome MATCHES the fixture's should_trigger expectation:
    a should_trigger=true fixture passes when the skill fired, and a
    should_trigger=false fixture passes when it didn't. Earlier
    versions returned `pass = triggered`, which made correct declines
    render as red on the dashboard's per-run cells. kind_extra always
    carries the raw `triggered` boolean so callers that need the
    underlying signal can read it directly.
    """
    parsed = parse_transcript(
        transcript_path,
        agent=bail.get("agent", DEFAULT_AGENT),
        known_skills=(target_skill,),
    )
    first_tool = parsed.tool_uses[0].name if parsed.tool_uses else None
    first_action = parsed.first_action.name if parsed.first_action else None
    first_skill = parsed.first_skill
    triggered = (first_action == "skill" and first_skill == target_skill)
    matched_expected = (triggered == fixture.get("should_trigger", True))
    return matched_expected, {
        "triggered": triggered,
        "first_tool": first_tool,
        "first_action": first_action,
        "first_skill": first_skill,
    }


def build_argument_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("-e", "--eval", required=True,
                    help="Path to trigger-eval.json")
    ap.add_argument("-s", "--skill-path", required=False,
                    help="Path to the skill directory (containing SKILL.md). "
                         "Required for the default 'isolated' profile; the "
                         "skill name is read from SKILL.md frontmatter. "
                         "Optional for 'restricted' or 'inherit' profiles, "
                         "which test the user's globally-installed skill.")
    ap.add_argument("-i", "--also-install", action="append", default=[],
                    metavar="PATH",
                    help="Path to a sibling skill to install alongside the "
                         "skill under test. May be repeated. Only effective "
                         "under the 'isolated' profile.")
    ap.add_argument("--skill-name", required=False, default=None,
                    help="Override the skill name. Default: read from "
                         "SKILL.md frontmatter when --skill-path is given.")
    ap.add_argument("-r", "--runs", type=int, default=3)
    ap.add_argument("-w", "--workers", type=int, default=4)
    ap.add_argument("-t", "--timeout", type=int, default=300,
                    help="Per-run wall-clock backstop in seconds "
                         "(default 300). Measures effective model-thinking "
                         "time; adapter-reported retry-backoff windows are "
                         "excluded.")
    ap.add_argument("-c", "--cwd", default=None,
                    help="CWD for agent subprocesses (default: current dir)")
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
    ap.add_argument("-o", "--out", required=True)
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
        if not args.skill_name:
            ap.error(
                "--skill-name is required when --skill-path is omitted "
                "(profile=restricted or profile=inherit)"
            )
        skill_path = None
        skill_name = args.skill_name
    also_install = [os.path.abspath(p) for p in args.also_install]

    os.environ["STREAM_EVAL_PROFILE"] = args.profile

    install_signal_handlers()
    sock_path = f"/tmp/stream-eval-{os.getpid()}.sock"
    threading.Thread(
        target=serve_socket, args=(sock_path,), daemon=True
    ).start()

    try:
        cwd = args.cwd or os.getcwd()
        with open(args.eval) as f:
            queries = json.load(f)
        try:
            validate_fixtures(queries)
        except FixtureSchemaError as e:
            print(f"FIXTURE SCHEMA ERROR: {e}", file=sys.stderr)
            return 2

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_dir = transcript_dir_for(out_path)

        score_callback = functools.partial(
            score_trigger_run, target_skill=skill_name,
        )

        def summarize(fixtures_with_runs):
            summary = []
            for entry in fixtures_with_runs:
                fx = entry["fixture"]
                runs = entry["runs"]
                # Read `triggered` from kind_extra, NOT pass_. As of the
                # green-means-correct fix, pass_ is "matched expected"
                # (correct decline => True), so summing pass_ would
                # conflate fires with correct declines. The trigger
                # rate is specifically about whether the skill fired.
                triggers = sum(
                    1 for r in runs
                    if (r.get("kind_extra") or {}).get("triggered")
                )
                rate = triggers / len(runs) if runs else 0
                did_pass = (
                    (rate >= 0.5) if fx["should_trigger"]
                    else (rate < 0.5)
                )
                summary.append({
                    "query": fx["query"],
                    "should_trigger": fx["should_trigger"],
                    "triggers": triggers,
                    "runs": len(runs),
                    "pass": did_pass,
                    "run_details": runs,
                })
            return summary

        results, exit_code = run_eval(
            kind="trigger",
            fixtures=queries,
            get_fixture_id=lambda fx: fx.get("name"),
            get_query=get_trigger_query,
            score_run=score_callback,
            summarize=summarize,
            runs_per_fixture=args.runs,
            workers=args.workers,
            timeout=args.timeout,
            cwd=cwd,
            transcript_dir=transcript_dir,
            summary_label="queries",
            skill_name=skill_name,
            eval_path=args.eval,
            skill_path=skill_path,
            also_install=also_install,
            agent=args.agent,
        )

        results["skill_name"] = skill_name
        results["passed"] = sum(
            1 for r in results["results"] if r["pass"]
        )
        results["failed"] = (
            len(results["results"]) - results["passed"]
        )

        with open(out_path, "w") as f:
            json.dump(results, f, indent=2)

        return exit_code
    finally:
        # Always remove the socket file, even on exception paths. The
        # daemon serve_socket thread is killed at process exit without
        # running its own finally block.
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

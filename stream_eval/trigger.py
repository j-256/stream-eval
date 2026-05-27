#!/usr/bin/env python3
"""Trigger-accuracy eval harness for skills.

Fires claude -p runs and scores the first tool invocation. Was
probe-eval.py historically (commit 1d1c08b); the rename matches the
fixture format (`evals/<skill>/trigger-eval.json`) and disambiguates
from stream_eval.synthesis.

For each query in the eval set, spawn N runs of `claude -p --model sonnet
<query>` in parallel. Parse the stream-json output; a run counts as
"triggered" iff the first tool_use event is the `Skill` tool with input
`{"skill": "<target-skill>"}`. Anything else (different skill, different
tool, text-only answer) counts as "not triggered."

A query passes when its trigger rate meets its `should_trigger` expectation
with a 0.5 threshold.

Prerequisite: the skill must already be installed under ~/.claude/skills/
with its real (clean) name. (Phase C of the extraction plan replaces this
with hermetic per-spawn isolation -- see docs/.)

Bail signal is api_retry-aware. The CLI emits stream-json events of the
shape `{"type":"system","subtype":"api_retry","attempt":N,"max_retries":M,
"error":"rate_limit"|"server_error",...}` while waiting on the gateway.
The harness streams the JSONL live and treats CLI internal retries as
"waiting on gateway, not the model thinking" -- they don't count against
the model-thinking timeout. A run aborts only when the CLI's full retry
budget is exhausted (attempt == max_retries on the most recent retry
event), which is the documented "gateway window is poisoned" condition.
A generous absolute wall clock (--timeout) acts as a safety backstop for
truly hung processes.

Exit codes mirror stream_eval.synthesis:
  0 -- all queries pass
  1 -- at least one query fails
  3 -- aborted on retry-budget exhaustion or absolute wall clock (no
       results.json written; throttle-corrupted partial data was the
       exact misleading state the abort is preventing -- re-run when the
       gateway has recovered)

Usage:
  stream-eval trigger \\
    --eval evals/dsc-endpoint-help/trigger-eval.json \\
    --skill-name dsc-endpoint-help \\
    --runs 3 --workers 4 --timeout 1800 \\
    --out evals/dsc-endpoint-help/runs/iteration-N/results.json
"""
import argparse
import functools
import json
import os
import threading
from pathlib import Path

from stream_eval.control import install_signal_handlers, serve_socket
from stream_eval.runner import run_eval


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
    and the bail dict. The runner doesn't call this on timed-out runs.

    Returns (pass: bool, kind_extra: dict). pass=True iff the first
    tool_use in the transcript is the Skill tool with input matching
    target_skill.
    """
    first_tool = None
    first_skill = None
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            for c in d.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    first_tool = c.get("name")
                    if first_tool == "Skill":
                        first_skill = c.get("input", {}).get("skill", "")
                    break
            if first_tool is not None:
                break

    triggered = (first_tool == "Skill" and first_skill == target_skill)
    return triggered, {
        "triggered": triggered,
        "first_tool": first_tool,
        "first_skill": first_skill,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", required=True, help="Path to trigger-eval.json")
    ap.add_argument("--skill-path", required=False,
                    help="Path to the skill directory (containing SKILL.md). "
                         "Required for the default 'isolated' profile; the "
                         "skill name is read from SKILL.md frontmatter. "
                         "Optional for 'restricted' or 'inherit' profiles, "
                         "which test the user's globally-installed skill.")
    ap.add_argument("--also-install", action="append", default=[],
                    metavar="PATH",
                    help="Path to a sibling skill to install alongside the "
                         "skill under test. May be repeated. Only effective "
                         "under the 'isolated' profile.")
    ap.add_argument("--skill-name", required=False, default=None,
                    help="Override the skill name. Default: read from "
                         "SKILL.md frontmatter when --skill-path is given.")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=1800,
                    help="Absolute wall-clock backstop in seconds (default "
                         "1800). Primary bail signal is api_retry budget "
                         "exhaustion; this only fires for a hung process.")
    ap.add_argument("--cwd", default=None,
                    help="CWD for claude -p subprocesses (default: current dir)")
    ap.add_argument(
        "--profile", choices=["isolated", "restricted", "inherit"],
        default="isolated",
        help="Toolbelt profile for the spawned claude -p. 'isolated' "
             "(default) uses a temp HOME with only the skill under test; "
             "'restricted' uses the user's real HOME but strips MCP/Agent; "
             "'inherit' runs with the user's full environment.",
    )
    ap.add_argument("--out", required=True)
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

    cwd = args.cwd or os.getcwd()
    queries = json.load(open(args.eval))

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
            # score_trigger_run sets pass_ = triggered, so the runner's
            # canonical pass_ field is the per-run trigger result. Use
            # it directly rather than re-reading kind_extra.triggered.
            triggers = sum(1 for r in runs if r["pass_"])
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

    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

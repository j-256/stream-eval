"""Unit tests for stream_eval/runner.py.

Run with: pytest tests/test_runner.py
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from stream_eval.runner import (
    assign_fixture_ids,
    FixtureSchemaError,
    _capture_transcript_artifacts,
    _dsc_reference_urls,
    _format_progress,
    _prewarm_dsc_cache,
    PROGRESS_LINE_RE,
    FINISH_BANNER_RE,
    format_finish_banner,
    format_startup_banner,
    STARTUP_BANNER_RE,
)


def test_capture_transcript_artifacts_reads_codex_temp_file(tmp_path):
    with tempfile.TemporaryDirectory(
        prefix="stream-eval-artifact-",
    ) as output_dir:
        output = Path(output_dir) / "demo.sh"
        output.write_text("#!/bin/sh\nprintf demo\n")
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [{"path": str(output), "kind": "add"}],
                },
            }) + "\n"
        )

        artifacts = _capture_transcript_artifacts(
            transcript,
            agent="codex",
            cwd=tmp_path / "unrelated-worktree",
        )

        assert artifacts == [{
            "path": str(output),
            "content": "#!/bin/sh\nprintf demo\n",
        }]


class TestPrewarmDscCache(unittest.TestCase):
    """The pre-warm scans fixture queries for DSC reference URLs and warms them
    serially before fan-out; it is a no-op when the skill ships no scrape lib or
    the fixtures name no URLs."""

    def _skill_with_scrape(self, tmp):
        d = Path(tmp) / "skill"
        (d / "lib" / "scrape").mkdir(parents=True)
        (d / "lib" / "scrape" / "scrape.js").write_text("// stub")
        return str(d)

    def test_scans_dedups_strips_and_scrapes_named_refs(self):
        import tempfile
        from unittest.mock import patch
        base = "https://developer.salesforce.com/docs/commerce/commerce-api/references"
        fixtures = [
            {"query": f"why does {base}/shopper-orders?meta=createOrder 400?"},
            {"query": f"prereqs, see {base}/shopper-orders"},        # dup root
            {"query": f"and {base}/shopper-baskets-v2, thanks"},
        ]
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            skill = self._skill_with_scrape(tmp)
            with patch("stream_eval.runner.subprocess.run",
                       side_effect=lambda cmd, **kw: calls.append(cmd)), \
                 patch("stream_eval.runner.ISOLATED_CACHE_DIR", str(Path(tmp) / "cache")), \
                 patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "isolated"}):
                _prewarm_dsc_cache(fixtures, lambda fx: fx["query"], skill)
        urls = [c[2] for c in calls]
        self.assertEqual(
            sorted(urls),
            sorted([f"{base}/shopper-orders", f"{base}/shopper-baskets-v2"]),
            f"?meta stripped, deduped to reference roots; got {urls}",
        )
        self.assertTrue(
            all(c[0] == "node" and c[1].endswith("scrape.js") for c in calls),
            "each warm invokes node on the skill's scrape.js",
        )

    def test_noop_when_skill_lacks_scrape_lib(self):
        from unittest.mock import patch
        base = "https://developer.salesforce.com/docs/commerce/commerce-api/references"
        with patch("stream_eval.runner.subprocess.run") as run:
            _prewarm_dsc_cache([{"query": f"{base}/shopper-orders"}],
                               lambda fx: fx["query"], "/nonexistent/skill")
            run.assert_not_called()

    def test_noop_when_no_urls(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            skill = self._skill_with_scrape(tmp)
            with patch("stream_eval.runner.subprocess.run") as run:
                _prewarm_dsc_cache([{"query": "prose about shopper-orders, no url"}],
                                   lambda fx: fx["query"], skill)
                run.assert_not_called()

    def test_reference_parser_handles_punctuation_and_adversarial_input(self):
        base = "https://developer.salesforce.com/docs/commerce/commerce-api/references"
        query = (
            f"See ({base}/shopper-orders?meta=createOrder), then "
            + "https://developer.salesforce.com/docs/" * 10_000
        )

        self.assertEqual(
            list(_dsc_reference_urls(query)),
            [f"{base}/shopper-orders"],
        )


class TestAssignFixtureIds(unittest.TestCase):
    def test_all_named(self):
        fixtures = [
            {"name": "alpha", "query": "q1"},
            {"name": "beta", "query": "q2"},
        ]
        result = assign_fixture_ids(fixtures, lambda fx: fx.get("name"))
        self.assertEqual(
            [fid for fid, _ in result],
            ["alpha", "beta"],
        )

    def test_all_anonymous_falls_back_to_index(self):
        fixtures = [{"query": "q1"}, {"query": "q2"}, {"query": "q3"}]
        result = assign_fixture_ids(fixtures, lambda fx: fx.get("name"))
        self.assertEqual(
            [fid for fid, _ in result],
            ["q0", "q1", "q2"],
        )

    def test_mixed_named_and_anonymous_skips_collisions(self):
        """A hand-authored name 'q3' must not collide with the auto-q3 slot.
        Auto-ids assign in input order, taking the lowest unused qN at each
        anonymous slot. With one explicit q3, the anonymous slots get
        q0, q1, q2 in order."""
        fixtures = [
            {"query": "q-anon-0"},
            {"name": "q3", "query": "q-named"},
            {"query": "q-anon-2"},
            {"query": "q-anon-3"},
        ]
        result = assign_fixture_ids(fixtures, lambda fx: fx.get("name"))
        ids = [fid for fid, _ in result]
        self.assertEqual(ids, ["q0", "q3", "q1", "q2"])

    def test_explicit_low_index_leapfrogs_anonymous(self):
        """When an explicit name shadows a low qN slot, the inner-loop
        skip-collision branch must advance next_idx past it. Catches
        regressions in the `while f'q{next_idx}' in explicit_set` body."""
        fixtures = [
            {"query": "q-anon-0"},          # would naturally take q0
            {"name": "q0", "query": "q-named"},
            {"query": "q-anon-2"},
        ]
        result = assign_fixture_ids(fixtures, lambda fx: fx.get("name"))
        ids = [fid for fid, _ in result]
        # First anonymous can't be q0 (reserved by fixture[1]); it
        # gets q1. Explicit q0 stays. Last anonymous gets q2.
        self.assertEqual(ids, ["q1", "q0", "q2"])

    def test_two_explicit_qN_names_both_reserved(self):
        """Two explicit names, both q-style, distributed across the
        list. Auto-ids must skip BOTH."""
        fixtures = [
            {"query": "a"},                  # anonymous
            {"name": "q0", "query": "b"},
            {"query": "c"},                  # anonymous
            {"name": "q5", "query": "d"},
            {"query": "e"},                  # anonymous
        ]
        result = assign_fixture_ids(fixtures, lambda fx: fx.get("name"))
        ids = [fid for fid, _ in result]
        # Anonymous slots take q1, q2, q3 (q0 and q5 reserved).
        self.assertEqual(ids, ["q1", "q0", "q2", "q5", "q3"])

    def test_duplicate_explicit_names_raise(self):
        fixtures = [
            {"name": "alpha", "query": "q1"},
            {"name": "alpha", "query": "q2"},
        ]
        with self.assertRaises(FixtureSchemaError) as ctx:
            assign_fixture_ids(fixtures, lambda fx: fx.get("name"))
        self.assertIn("alpha", str(ctx.exception))

    def test_empty_string_name_treated_as_anonymous(self):
        fixtures = [{"name": "", "query": "q1"}, {"name": "real", "query": "q2"}]
        result = assign_fixture_ids(fixtures, lambda fx: fx.get("name"))
        self.assertEqual(
            [fid for fid, _ in result],
            ["q0", "real"],
        )

    def test_none_name_treated_as_anonymous(self):
        fixtures = [{"name": None, "query": "q1"}]
        result = assign_fixture_ids(fixtures, lambda fx: fx.get("name"))
        self.assertEqual(
            [fid for fid, _ in result],
            ["q0"],
        )


class TestProgressLineRoundTrip(unittest.TestCase):
    def _round_trip(self, record):
        """Format then parse a record; return the parsed groups.
        Required fields default to no-op sentinels so each test only
        sets what it needs to assert."""
        defaults = {
            "timeout_reason": "none",
            "first_tool": "-",
            "first_skill": "-",
            "failed_asserts": 0,
            "contaminated": False,
        }
        merged = {**defaults, **record}
        line = _format_progress(
            n=merged["n"],
            total=merged["total"],
            kind=merged["kind"],
            pass_=merged["pass_"],
            fixture_id=merged["fixture_id"],
            run_idx=merged["run_idx"],
            elapsed_seconds=merged["elapsed_seconds"],
            total_retries=merged["total_retries"],
            timeout_reason=merged["timeout_reason"],
            first_tool=merged["first_tool"],
            first_skill=merged["first_skill"],
            failed_asserts=merged["failed_asserts"],
            contaminated=merged["contaminated"],
            query=merged["query"],
        )
        m = PROGRESS_LINE_RE.search(line)
        self.assertIsNotNone(m, f"regex did not match line: {line!r}")
        return m.groupdict()

    def test_trigger_pass_line(self):
        groups = self._round_trip({
            "n": 34, "total": 69, "kind": "trigger", "pass_": True,
            "fixture_id": "q12", "run_idx": 2, "elapsed_seconds": 42.1,
            "total_retries": 2,
            "first_tool": "Skill", "first_skill": "dsc-triage",
            "query": "what scopes does X need?",
        })
        self.assertEqual(groups["n"], "34")
        self.assertEqual(groups["total"], "69")
        self.assertEqual(groups["kind"], "trigger")
        self.assertEqual(groups["pass_"], "True")
        self.assertEqual(groups["fixture_id"], "q12")
        self.assertEqual(groups["run"], "2")
        self.assertEqual(groups["elapsed"], "42.1")
        self.assertEqual(groups["retries"], "2")
        self.assertEqual(groups["timeout_reason"], "none")
        self.assertEqual(groups["first_tool"], "Skill")
        self.assertEqual(groups["first_skill"], "dsc-triage")
        self.assertEqual(groups["failed_asserts"], "0")
        self.assertEqual(groups["query"], "what scopes does X need?")

    def test_trigger_fail_wrong_tool(self):
        """Trigger run that went straight to Bash instead of Skill --
        first_tool diagnoses what went wrong."""
        groups = self._round_trip({
            "n": 5, "total": 60, "kind": "trigger", "pass_": False,
            "fixture_id": "q4", "run_idx": 1, "elapsed_seconds": 12.0,
            "total_retries": 0,
            "first_tool": "Bash", "first_skill": "-",
            "query": "list every endpoint",
        })
        self.assertEqual(groups["pass_"], "False")
        self.assertEqual(groups["first_tool"], "Bash")
        self.assertEqual(groups["first_skill"], "-")

    def test_synthesis_fail_with_assertion_failures(self):
        groups = self._round_trip({
            "n": 7, "total": 10, "kind": "synthesis", "pass_": False,
            "fixture_id": "mcg-citation-leak", "run_idx": 3,
            "elapsed_seconds": 87.4, "total_retries": 0,
            "first_tool": "Skill", "first_skill": "dsc-scrape",
            "failed_asserts": 2,
            "query": "find the MCG reference",
        })
        self.assertEqual(groups["kind"], "synthesis")
        self.assertEqual(groups["pass_"], "False")
        self.assertEqual(groups["fixture_id"], "mcg-citation-leak")
        self.assertEqual(groups["failed_asserts"], "2")

    def test_timeout_reason_retry_budget(self):
        """Timed-out runs report which timeout fired."""
        groups = self._round_trip({
            "n": 3, "total": 10, "kind": "trigger", "pass_": False,
            "fixture_id": "q2", "run_idx": 1, "elapsed_seconds": 1800.0,
            "total_retries": 10, "timeout_reason": "retry_budget",
            "first_tool": "-", "first_skill": "-",
            "query": "...",
        })
        self.assertEqual(groups["timeout_reason"], "retry_budget")

    def test_timeout_reason_wall_clock_in_retry(self):
        """Stuck-during-retry case: the absolute backstop (4 * timeout)
        fired while the CLI was wedged inside a retry sleep with no
        further events. Distinct from regular wall_clock so operators
        can triage skill-stuck vs. retry-mechanism-stuck after the fact."""
        groups = self._round_trip({
            "n": 8, "total": 10, "kind": "synthesis", "pass_": False,
            "fixture_id": "q4", "run_idx": 2, "elapsed_seconds": 2400.0,
            "total_retries": 1, "timeout_reason": "wall_clock_in_retry",
            "first_tool": "-", "first_skill": "-",
            "query": "...",
        })
        self.assertEqual(groups["timeout_reason"], "wall_clock_in_retry")

    def test_query_truncation_to_80_chars(self):
        long_q = "x" * 200
        groups = self._round_trip({
            "n": 1, "total": 1, "kind": "trigger", "pass_": True,
            "fixture_id": "q0", "run_idx": 1, "elapsed_seconds": 1.0,
            "total_retries": 0, "query": long_q,
        })
        self.assertEqual(len(groups["query"]), 80)

    def test_query_with_newline_normalized(self):
        groups = self._round_trip({
            "n": 1, "total": 1, "kind": "trigger", "pass_": True,
            "fixture_id": "q0", "run_idx": 1, "elapsed_seconds": 1.0,
            "total_retries": 0,
            "query": "line one\nline two",
        })
        self.assertNotIn("\n", groups["query"])
        self.assertIn("line one line two", groups["query"])


class TestStartupBanner(unittest.TestCase):
    def test_banner_round_trips(self):
        line = format_startup_banner(
            kind="trigger",
            skill="dsc-triage",
            eval_path="evals/dsc-triage/trigger-eval.json",
            runs=3, workers=4, total_fixtures=23, pid=12345,
        )
        m = STARTUP_BANNER_RE.search(line)
        self.assertIsNotNone(m, f"banner regex did not match: {line!r}")
        groups = m.groupdict()
        self.assertEqual(groups["kind"], "trigger")
        self.assertEqual(groups["skill"], "dsc-triage")
        self.assertEqual(
            groups["eval"],
            "evals/dsc-triage/trigger-eval.json",
        )
        self.assertEqual(groups["runs"], "3")
        self.assertEqual(groups["workers"], "4")
        self.assertEqual(groups["total_fixtures"], "23")
        self.assertEqual(groups["pid"], "12345")

    def test_banner_handles_synthesis_kind(self):
        line = format_startup_banner(
            kind="synthesis",
            skill="dsc-scrape",
            eval_path="evals/dsc-scrape/synthesis-eval.json",
            runs=5, workers=4, total_fixtures=2, pid=99999,
        )
        m = STARTUP_BANNER_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("kind"), "synthesis")
        self.assertEqual(m.group("total_fixtures"), "2")
        self.assertEqual(m.group("pid"), "99999")

    def test_banner_pid_defaults_to_current_process(self):
        """When called without an explicit pid, the banner stamps
        os.getpid() so the dashboard can bind .output files to the
        right harness process without callers having to remember."""
        import os
        line = format_startup_banner(
            kind="trigger",
            skill="dsc-scrape",
            eval_path="evals/dsc-scrape/trigger-eval.json",
            runs=1, workers=1, total_fixtures=1,
        )
        m = STARTUP_BANNER_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("pid"), str(os.getpid()))

    def test_banner_legacy_form_without_pid_still_parses(self):
        """Banners written by tools/ before F.5 don't carry pid=. The
        regex must still match so existing .output files survive a
        re-parse without crashing the dashboard."""
        legacy = (
            "=== eval starting: kind=trigger skill=dsc-scrape "
            "eval=evals/dsc-scrape/trigger-eval.json "
            "runs=3 workers=4 total_fixtures=10 ==="
        )
        m = STARTUP_BANNER_RE.search(legacy)
        self.assertIsNotNone(m)
        self.assertIsNone(m.group("pid"))

    def test_banner_shaped_substring_does_not_match(self):
        """A banner-shaped substring embedded inside a longer line (e.g.
        printed by a subagent or a prompt copy-paste) must NOT match.
        Real banners always start at column 0."""
        embedded = (
            'echo "fixture text: === eval starting: kind=trigger '
            'skill=fake eval=evals/fake/trigger-eval.json '
            'runs=3 workers=4 total_fixtures=10 ==="'
        )
        self.assertIsNone(STARTUP_BANNER_RE.search(embedded))

    def test_banner_includes_started_at_timestamp(self):
        """New banners stamp started_at as a unix timestamp -- the
        dashboard's runtime indicator reads this rather than file
        ctime/mtime, which has portability problems
        (st_ctime: macOS=creation, Linux=inode-change)."""
        line = format_startup_banner(
            kind="trigger", skill="dsc-scrape",
            eval_path="evals/dsc-scrape/trigger-eval.json",
            runs=1, workers=1, total_fixtures=1, pid=42,
            started_at=1780_000_000.123,
        )
        m = STARTUP_BANNER_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("started_at"), "1780000000.123")

    def test_banner_started_at_defaults_to_now(self):
        """When called without started_at, format_startup_banner stamps
        time.time() so callers don't have to remember.

        The banner formats with millisecond precision (%.3f); allow a
        2ms tolerance on the bounds-check to absorb the rounding."""
        import time
        before = time.time()
        line = format_startup_banner(
            kind="trigger", skill="x",
            eval_path="e.json",
            runs=1, workers=1, total_fixtures=1, pid=1,
        )
        after = time.time()
        m = STARTUP_BANNER_RE.search(line)
        self.assertIsNotNone(m)
        ts = float(m.group("started_at"))
        self.assertGreaterEqual(ts, before - 0.002)
        self.assertLessEqual(ts, after + 0.002)

    def test_banner_legacy_form_without_started_at_still_parses(self):
        """Banners written before the started_at field still parse;
        groupdict()['started_at'] is None for those."""
        legacy = (
            "=== eval starting: kind=trigger skill=dsc-scrape "
            "eval=evals/dsc-scrape/trigger-eval.json "
            "runs=3 workers=4 total_fixtures=10 pid=42 ==="
        )
        m = STARTUP_BANNER_RE.search(legacy)
        self.assertIsNotNone(m)
        self.assertIsNone(m.group("started_at"))


class TestFinishBanner(unittest.TestCase):
    def test_finish_completed_round_trips(self):
        line = format_finish_banner(
            kind="trigger", skill="dsc-scrape",
            verdict="completed", pid=12345,
        )
        m = FINISH_BANNER_RE.search(line)
        self.assertIsNotNone(m, f"finish regex did not match: {line!r}")
        self.assertEqual(m.group("kind"), "trigger")
        self.assertEqual(m.group("skill"), "dsc-scrape")
        self.assertEqual(m.group("pid"), "12345")
        self.assertEqual(m.group("verdict"), "completed")

    def test_finish_aborted_round_trips(self):
        line = format_finish_banner(
            kind="synthesis", skill="dsc-scenario",
            verdict="aborted", pid=99999,
        )
        m = FINISH_BANNER_RE.search(line)
        self.assertEqual(m.group("verdict"), "aborted")

    def test_finish_pid_defaults_to_current_process(self):
        import os
        line = format_finish_banner(
            kind="trigger", skill="x", verdict="completed",
        )
        m = FINISH_BANNER_RE.search(line)
        self.assertEqual(m.group("pid"), str(os.getpid()))

    def test_finish_does_not_match_startup(self):
        startup = (
            "=== eval starting: kind=trigger skill=dsc-scrape "
            "eval=evals/dsc-scrape/trigger-eval.json "
            "runs=3 workers=4 total_fixtures=10 pid=42 ==="
        )
        self.assertIsNone(FINISH_BANNER_RE.search(startup))

    def test_finish_includes_finished_at_timestamp(self):
        """Finish banner stamps finished_at -- the dashboard pairs it
        with started_at on the startup banner to render total runtime
        on completed/aborted rows."""
        line = format_finish_banner(
            kind="trigger", skill="x", verdict="completed", pid=42,
            finished_at=1780_000_300.456,
        )
        m = FINISH_BANNER_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("finished_at"), "1780000300.456")

    def test_finish_legacy_form_without_finished_at_still_parses(self):
        """Banners written before the finished_at field still parse;
        groupdict()['finished_at'] is None for those."""
        legacy = (
            "=== eval finished: kind=trigger skill=x "
            "pid=42 verdict=completed ==="
        )
        m = FINISH_BANNER_RE.search(legacy)
        self.assertIsNotNone(m)
        self.assertIsNone(m.group("finished_at"))


import threading
import time
import unittest.mock as mock
from concurrent.futures import ThreadPoolExecutor
from stream_eval.runner import run_eval


class TestRunEvalAbortOnTimeout(unittest.TestCase):
    """The runner must cancel pending futures and exit 3 when a completed
    run reports an upstream-poisoned timeout. Validates the abort policy
    without spawning real agent CLI subprocesses.

    Tests pass executor_class=ThreadPoolExecutor so mock.patch reaches
    the workers (process-pool workers run in separate processes and
    don't see parent-process patches).

    Determinism note: with ThreadPoolExecutor and workers=1, the worker
    thread pulls the next queued task immediately after each call
    returns -- it does NOT yield to the main thread between tasks. By
    the time the main thread receives the first result via
    as_completed and calls cancel() on pending futures, the worker has
    typically already pulled task 2. Cancel honors not-yet-pulled
    futures (returns True for them, the executor skips them at
    shutdown), but a future that has already been pulled by the worker
    runs to completion regardless of cancel().

    Net effect: scored_calls contains the timed-out task plus AT MOST
    one already-pulled task (=2). If cancellation is broken entirely,
    all 6 tasks run. The test asserts <= 2 to capture the abort policy
    deterministically without timing dependence; the second test
    (envelope shape) covers the abort path's bookkeeping."""

    def test_abort_cancels_remaining_runs(self):
        # Three fixtures, runs_per_fixture=2 -> six tasks. The first
        # scored task reports retry-budget exhaustion; the runner must cancel
        # remaining futures so far fewer than six run total.
        # Subsequent mock calls block on a gate to remain reliably
        # cancellable: without the gate, fast mock returns let the
        # worker drain the entire queue before main thread can cancel.
        fixtures = [{"q": "a"}, {"q": "b"}, {"q": "c"}]
        scored_calls = []
        # Subsequent (non-first) calls block on this gate until the
        # test releases them at the end. With workers=1, the worker
        # can only have one such blocked call active at a time, and
        # the rest stay pending and cancellable.
        gate = threading.Event()

        def fake_runner(fixture, run_idx, fixture_id, transcript_dir,
                        timeout, cwd, get_query, score_run,
                        skill_path=None, also_install=()):
            """Simulates one worker: returns a per-run record dict
            that the runner reads. The first call reports timed_out;
            subsequent calls (if reached, before cancellation) block
            on the gate so they can be cancelled / drained at shutdown.

            Signature mirrors the real _run_one_task so the mock is a
            drop-in replacement."""
            is_first = len(scored_calls) == 0
            scored_calls.append((fixture_id, run_idx))
            if not is_first:
                # Block until the test releases. Bounded wait keeps
                # the test from hanging if something goes wrong.
                gate.wait(timeout=5.0)
            return {
                "fixture_id": fixture_id,
                "run_idx": run_idx,
                "elapsed_seconds": 1.0,
                "total_retries": 0,
                "timed_out": is_first,
                "timeout_reason": "retry_budget_exhausted" if is_first else None,
                "transcript_path": None,
                "pass_": not is_first,
                "kind_extra": {},
            }

        # Release the gate from a watchdog thread so any task that the
        # worker pulled before cancel landed can complete and let
        # shutdown(wait=True) return. The release is delayed enough
        # that the main thread has already issued cancel() on pending
        # futures.
        def release_after(delay):
            time.sleep(delay)
            gate.set()

        watchdog = threading.Thread(target=release_after, args=(0.2,))
        watchdog.start()

        try:
            with tempfile.TemporaryDirectory() as td:
                with mock.patch("stream_eval.runner._run_one_task", side_effect=fake_runner):
                    results, exit_code = run_eval(
                        kind="trigger",
                        fixtures=fixtures,
                        get_fixture_id=lambda fx: None,
                        get_query=lambda fx: fx["q"],
                        score_run=None,  # not reached -- _run_one_task is mocked
                        summarize=lambda fixtures_with_runs: [],
                        runs_per_fixture=2,
                        workers=1,
                        timeout=60,
                        cwd=str(td),
                        transcript_dir=None,
                        summary_label="queries",
                        skill_name="test-skill",
                        eval_path="evals/test/trigger-eval.json",
                        executor_class=ThreadPoolExecutor,
                    )
        finally:
            gate.set()  # belt-and-suspenders
            watchdog.join(timeout=1.0)

        self.assertEqual(exit_code, 3, f"expected abort exit 3, got {exit_code}")
        self.assertTrue(results.get("aborted_on_timeout"))
        # See class docstring: the timed-out task plus at most one
        # already-pulled task may run; the remaining (>=4 of 6) must
        # be cancelled. If cancellation is broken entirely, all 6 run.
        self.assertLessEqual(
            len(scored_calls), 2,
            f"abort failed to cancel: {len(scored_calls)} runs completed "
            f"(expected <= 2)",
        )
        self.assertGreaterEqual(
            len(scored_calls), 1,
            "first run should have scored before abort fired",
        )

    def test_dispatch_order_is_run_major(self):
        """Tasks must be dispatched run-major (round-robin by run), not
        fixture-major. Given 3 fixtures x 2 runs = 6 tasks, the worker
        must see (fx0,r1) (fx1,r1) (fx2,r1) (fx0,r2) (fx1,r2) (fx2,r2),
        not (fx0,r1) (fx0,r2) (fx1,r1) (fx1,r2) (fx2,r1) (fx2,r2).
        Run-major ordering ensures partial coverage measures every
        fixture once before any fixture is re-measured.
        ThreadPoolExecutor + workers=1 makes dispatch order observable:
        the single worker pulls tasks in submission order."""
        fixtures = [
            {"name": "alpha", "q": "qa"},
            {"name": "beta", "q": "qb"},
            {"name": "gamma", "q": "qc"},
        ]
        scored_calls = []

        def fake_runner(fixture, run_idx, fixture_id, transcript_dir,
                        timeout, cwd, get_query, score_run,
                        skill_path=None, also_install=()):
            scored_calls.append((fixture_id, run_idx))
            return {
                "fixture_id": fixture_id, "run_idx": run_idx,
                "elapsed_seconds": 0.01, "total_retries": 0,
                "timed_out": False, "timeout_reason": None,
                "transcript_path": None, "pass_": True, "kind_extra": {},
            }

        with tempfile.TemporaryDirectory() as td:
            with mock.patch("stream_eval.runner._run_one_task", side_effect=fake_runner):
                run_eval(
                    kind="trigger",
                    fixtures=fixtures,
                    get_fixture_id=lambda fx: fx.get("name"),
                    get_query=lambda fx: fx["q"],
                    score_run=None,
                    summarize=lambda fixtures_with_runs: [
                        {"fixture_id": f["fixture_id"], "pass": True}
                        for f in fixtures_with_runs
                    ],
                    runs_per_fixture=2, workers=1, timeout=10,
                    cwd=str(td),
                    transcript_dir=None,
                    summary_label="queries",
                    skill_name="test-skill",
                    eval_path="evals/test/trigger-eval.json",
                    executor_class=ThreadPoolExecutor,
                )

        expected = [
            ("alpha", 1), ("beta", 1), ("gamma", 1),
            ("alpha", 2), ("beta", 2), ("gamma", 2),
        ]
        self.assertEqual(
            scored_calls, expected,
            f"expected run-major dispatch {expected}, got {scored_calls}",
        )

    def test_envelope_fields_present_on_abort(self):
        """Even on abort, the results dict has the runner-owned envelope
        fields populated (so a future iteration can opt to write partial
        results.json on abort -- not the current behavior, but the
        envelope shape should be ready for it).

        Uses retry_budget_exhausted as the abort trigger because
        wall_clock-without-retry no longer aborts the eval (it skips
        the fixture's remaining runs instead). Only retry-budget and
        wall_clock_in_retry are upstream-poisoned signals that abort."""
        fixtures = [{"q": "a"}]

        def fake_runner(fixture, run_idx, fixture_id, transcript_dir,
                        timeout, cwd, get_query, score_run,
                        skill_path=None, also_install=()):
            return {
                "fixture_id": fixture_id, "run_idx": run_idx,
                "elapsed_seconds": 0.5, "total_retries": 10,
                "timed_out": True, "timeout_reason": "retry_budget_exhausted",
                "transcript_path": None, "pass_": False, "kind_extra": {},
            }

        with tempfile.TemporaryDirectory() as td:
            with mock.patch("stream_eval.runner._run_one_task", side_effect=fake_runner):
                results, exit_code = run_eval(
                    kind="synthesis",
                    fixtures=fixtures,
                    get_fixture_id=lambda fx: None,
                    get_query=lambda fx: fx["q"],
                    score_run=None,
                    summarize=lambda fixtures_with_runs: [],
                    runs_per_fixture=1, workers=1, timeout=10,
                    cwd=str(td),
                    transcript_dir=None,
                    summary_label="fixtures",
                    skill_name="test-skill",
                    eval_path="evals/test/synthesis-eval.json",
                    executor_class=ThreadPoolExecutor,
                )

        for field in ("kind", "eval_set", "elapsed_seconds",
                      "aborted_on_timeout", "completed_runs",
                      "total_runs_planned", "results"):
            self.assertIn(field, results, f"envelope missing {field!r}")
        self.assertEqual(results["kind"], "synthesis")
        self.assertEqual(exit_code, 3)


import shutil
import subprocess
from stream_eval.runner import (
    _git_dirty_set, _git_repo_root, _diff_dirty_sets, _restore_worktree_paths,
)


class TestWorktreeIsolationPrimitives(unittest.TestCase):
    """End-to-end coverage of the snapshot/diff/restore primitives that
    _spawn_and_bail uses to detect and remediate eval-agent
    contamination. Each test materialises a real local git repo via
    `git init` and tests against it; the primitives talk to git directly
    rather than parsing porcelain in pure Python, so a real-repo fixture
    is the smallest-blast-radius way to verify their behavior matches
    git's actual semantics.

    Reason this lives in tools/test_eval_runner.py rather than under
    skills/_shared/tests/run.sh: this is harness/python plumbing, not
    skill-shared JS.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # Init repo with a baseline commit so HEAD exists.
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
            ["git", "config", "commit.gpgsign", "false"],
        ):
            subprocess.run(cmd, cwd=self.tmpdir, check=True,
                           capture_output=True)
        # Two tracked files + a .gitignore that excludes a runs/ dir.
        Path(self.tmpdir, "tracked.txt").write_text("HEAD content\n")
        Path(self.tmpdir, "skills").mkdir()
        Path(self.tmpdir, "skills", "module.js").write_text("export {};\n")
        Path(self.tmpdir, ".gitignore").write_text("runs/\n*.log\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=self.tmpdir, check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "baseline"],
            cwd=self.tmpdir, check=True, capture_output=True,
        )

    def test_dirty_set_empty_on_clean_repo(self):
        self.assertEqual(_git_dirty_set(self.tmpdir), set())

    def test_dirty_set_detects_modified_tracked_file(self):
        Path(self.tmpdir, "tracked.txt").write_text("MODIFIED\n")
        dirty = _git_dirty_set(self.tmpdir)
        self.assertIn("tracked.txt", dirty)

    def test_dirty_set_detects_new_untracked_file(self):
        Path(self.tmpdir, "skills", "newfile.js").write_text("// new\n")
        dirty = _git_dirty_set(self.tmpdir)
        self.assertIn("skills/newfile.js", dirty)

    def test_dirty_set_excludes_gitignored_paths(self):
        # Files matching .gitignore are never reported by git status,
        # so the eval harness's runs/ output dir won't trip the
        # contamination detector even when synthesis-eval writes its
        # results.json mid-run. Anchor: a missing exclusion here would
        # mean every successful eval reports itself as contaminated.
        Path(self.tmpdir, "runs").mkdir()
        Path(self.tmpdir, "runs", "results.json").write_text("{}\n")
        Path(self.tmpdir, "ephemeral.log").write_text("noise\n")
        self.assertEqual(_git_dirty_set(self.tmpdir), set())

    def test_diff_dirty_subtracts_baseline(self):
        """Operator's pre-existing dirty paths must NOT be flagged as
        contamination. The harness only treats *newly*-dirty paths
        (after - before) as eval-induced."""
        Path(self.tmpdir, "tracked.txt").write_text("operator's WIP\n")
        before = _git_dirty_set(self.tmpdir)
        # Eval run dirties an additional file.
        Path(self.tmpdir, "skills", "module.js").write_text("CONTAMINATED\n")
        after = _git_dirty_set(self.tmpdir)
        delta = _diff_dirty_sets(before, after)
        self.assertEqual(delta, {"skills/module.js"})
        self.assertNotIn("tracked.txt", delta)

    def test_restore_reverts_modified_tracked_file(self):
        Path(self.tmpdir, "skills", "module.js").write_text("CONTAMINATED\n")
        delta = {"skills/module.js"}
        failures = _restore_worktree_paths(self.tmpdir, delta)
        self.assertEqual(failures, [])
        self.assertEqual(
            Path(self.tmpdir, "skills", "module.js").read_text(),
            "export {};\n",
            "tracked file should have been reverted to HEAD content",
        )

    def test_restore_unlinks_newly_untracked_file(self):
        new_path = Path(self.tmpdir, "skills", "newfile.js")
        new_path.write_text("// eval-injected\n")
        delta = {"skills/newfile.js"}
        failures = _restore_worktree_paths(self.tmpdir, delta)
        self.assertEqual(failures, [])
        self.assertFalse(
            new_path.exists(),
            "newly-untracked file should have been unlinked",
        )

    def test_restore_handles_mixed_tracked_and_untracked(self):
        """The realistic contamination shape: one modified tracked file
        plus one new untracked file in the same run."""
        Path(self.tmpdir, "tracked.txt").write_text("CONTAMINATED\n")
        Path(self.tmpdir, "skills", "newfile.js").write_text("// new\n")
        delta = {"tracked.txt", "skills/newfile.js"}
        failures = _restore_worktree_paths(self.tmpdir, delta)
        self.assertEqual(failures, [])
        self.assertEqual(
            Path(self.tmpdir, "tracked.txt").read_text(), "HEAD content\n",
        )
        self.assertFalse(Path(self.tmpdir, "skills", "newfile.js").exists())
        self.assertEqual(
            _git_dirty_set(self.tmpdir), set(),
            "worktree should be back to clean after restore",
        )

    def test_restore_empty_delta_is_noop(self):
        failures = _restore_worktree_paths(self.tmpdir, set())
        self.assertEqual(failures, [])

    def test_restore_unlink_already_gone_is_silent(self):
        """If the contaminating Edit was already cleaned up before
        restore runs (rare race; safer-than-strict semantic), it must
        not fail the run."""
        delta = {"skills/never-existed.js"}
        failures = _restore_worktree_paths(self.tmpdir, delta)
        self.assertEqual(failures, [])

    def test_repo_root_resolves_from_subdirectory(self):
        sub = Path(self.tmpdir, "skills")
        self.assertEqual(
            os.path.realpath(_git_repo_root(str(sub))),
            os.path.realpath(self.tmpdir),
        )


class TestSpawnAndBailWorktreeProtection(unittest.TestCase):
    """End-to-end coverage of the snapshot/restore cycle wired into
    _spawn_and_bail. Patches run_with_retry_aware_bail to simulate a
    agent CLI spawn that mutates a tracked file mid-call (the
    eval-agent contamination shape iteration-resolve-slug-fallback-rejected
    diagnosed). Validates the integration the unit tests exercise in
    isolation: detection runs, restore runs, contamination flag and
    paths reach the bail dict.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
            ["git", "config", "commit.gpgsign", "false"],
        ):
            subprocess.run(cmd, cwd=self.tmpdir, check=True,
                           capture_output=True)
        Path(self.tmpdir, "skills").mkdir()
        self.victim = Path(self.tmpdir, "skills", "module.js")
        self.victim.write_text("export {};\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=self.tmpdir, check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "baseline"],
            cwd=self.tmpdir, check=True, capture_output=True,
        )

    def test_spawn_clean_run_reports_uncontaminated(self):
        from stream_eval.runner import _spawn_and_bail

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail",
                side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    bail = _spawn_and_bail(
                        "fake query", tf.name, timeout=30, cwd=self.tmpdir,
                    )

        self.assertFalse(bail["worktree_contaminated"])
        self.assertEqual(bail["worktree_changed_paths"], [])
        self.assertEqual(bail["worktree_restore_failures"], [])

    def test_spawn_baseline_subtracts_pre_existing_dirty_paths(self):
        """Operator's pre-existing dirty paths must NOT be flagged as
        contamination from an eval run that left them alone. This is
        what kept my own iteration-WIP edits to tools/_eval_runner.py
        from being reverted by the verification run."""
        from stream_eval.runner import _spawn_and_bail
        operator_wip = Path(self.tmpdir, "skills", "operator-wip.js")
        operator_wip.write_text("// in-flight edit\n")  # untracked, dirty

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail",
                side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    bail = _spawn_and_bail(
                        "fake query", tf.name, timeout=30, cwd=self.tmpdir,
                    )

        self.assertFalse(bail["worktree_contaminated"])
        self.assertTrue(
            operator_wip.exists(),
            "operator's pre-existing dirty file must NOT be touched",
        )


from stream_eval.runner import (
    _create_worker_worktree, _destroy_worker_worktree,
    _lock_worktree_readonly, _unlock_worktree,
)


class TestWorkerWorktreeLifecycle(unittest.TestCase):
    """Unit coverage for the per-spawn worktree create/destroy primitives.

    These replace the v2 detection-and-restore primitives that proved
    self-destructive: when an eval agent ran `git checkout -b` on the
    operator's repo, _restore_branch_state's `git checkout --force`
    discarded the operator's WIP -- including the harness source files
    being edited mid-development. Worktree-per-spawn makes that
    physically impossible: the spawn cwd is a separate checkout, not
    the operator's repo.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
            ["git", "config", "commit.gpgsign", "false"],
        ):
            subprocess.run(cmd, cwd=self.tmpdir, check=True,
                           capture_output=True)
        Path(self.tmpdir, "tracked.txt").write_text("HEAD content\n")
        Path(self.tmpdir, "skills").mkdir()
        Path(self.tmpdir, "skills", "module.js").write_text("export {};\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=self.tmpdir, check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "baseline"],
            cwd=self.tmpdir, check=True, capture_output=True,
        )

    def _operator_state(self):
        """Return (HEAD sha, branch list, worktree dirty set) for the
        operator repo. Tests assert these are byte-identical pre and
        post spawn -- the load-bearing isolation invariant."""
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        branches = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        dirty = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        ).stdout
        return head, sorted(branches), dirty

    def test_create_returns_existing_directory_outside_repo(self):
        wt = _create_worker_worktree(self.tmpdir, "test-spawn-1")
        self.addCleanup(_destroy_worker_worktree, wt)
        self.assertTrue(os.path.isdir(wt),
                        f"worktree path should exist: {wt}")
        # Path must be OUTSIDE the operator's repo so contamination
        # there can't reach repo-relative content.
        self.assertFalse(
            os.path.realpath(wt).startswith(os.path.realpath(self.tmpdir)),
            f"worktree {wt} must not be inside operator repo {self.tmpdir}",
        )

    def test_create_checks_out_head_content(self):
        wt = _create_worker_worktree(self.tmpdir, "test-spawn-2")
        self.addCleanup(_destroy_worker_worktree, wt)
        # Worktree starts at the operator repo's HEAD commit.
        self.assertEqual(
            Path(wt, "tracked.txt").read_text(), "HEAD content\n",
        )
        self.assertEqual(
            Path(wt, "skills", "module.js").read_text(), "export {};\n",
        )

    def test_create_does_not_include_operator_wip(self):
        """Worktree-at-HEAD: uncommitted edits in the operator repo
        must NOT appear in the worktree. This is the property that
        makes the harness un-self-eatable -- eval can't see in-flight
        v2 source edits at all, so it can't accidentally revert them."""
        Path(self.tmpdir, "tracked.txt").write_text("OPERATOR WIP\n")
        Path(self.tmpdir, "wip-untracked.txt").write_text("new file\n")
        wt = _create_worker_worktree(self.tmpdir, "test-spawn-3")
        self.addCleanup(_destroy_worker_worktree, wt)
        self.assertEqual(
            Path(wt, "tracked.txt").read_text(), "HEAD content\n",
            "WIP edit on tracked file should not appear in worktree",
        )
        self.assertFalse(
            Path(wt, "wip-untracked.txt").exists(),
            "WIP untracked file should not appear in worktree",
        )

    def test_destroy_removes_directory_and_unregisters_worktree(self):
        wt = _create_worker_worktree(self.tmpdir, "test-spawn-4")
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        )
        self.assertIn(wt, proc.stdout, "worktree should be registered")

        failures = _destroy_worker_worktree(wt)
        self.assertEqual(failures, [])
        self.assertFalse(
            os.path.exists(wt), "worktree dir should be gone",
        )
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        )
        self.assertNotIn(wt, proc.stdout,
                         "worktree should be unregistered")

    def test_destroy_succeeds_with_dirty_worktree(self):
        """Force-remove handles the contamination case: an eval agent
        edits/clones/branches inside the worktree, then we destroy.
        Without --force, `git worktree remove` would refuse on dirty
        state and leave the operator with stale registrations."""
        wt = _create_worker_worktree(self.tmpdir, "test-spawn-5")
        # Simulate contamination inside the worktree.
        Path(wt, "tracked.txt").write_text("CONTAMINATED\n")
        Path(wt, "phantom-clone").mkdir()
        Path(wt, "phantom-clone", "README").write_text("phantom\n")
        subprocess.run(["git", "checkout", "-b", "feat/phantom"],
                       cwd=wt, check=True, capture_output=True)

        failures = _destroy_worker_worktree(wt)
        self.assertEqual(failures, [])
        self.assertFalse(os.path.exists(wt))

    def test_operator_repo_unchanged_after_create_destroy(self):
        """Load-bearing invariant: the operator's repo state is
        byte-identical before and after a worktree lifecycle. This
        is what the v2 detection-and-restore design failed at."""
        before = self._operator_state()
        wt = _create_worker_worktree(self.tmpdir, "test-spawn-6")
        # Simulate eval contamination inside the worktree.
        Path(wt, "tracked.txt").write_text("CONTAMINATED\n")
        subprocess.run(["git", "checkout", "-b", "feat/phantom"],
                       cwd=wt, check=True, capture_output=True)
        Path(wt, "skills", "module.js").write_text("CONTAMINATED\n")
        subprocess.run(["git", "add", "-A"],
                       cwd=wt, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "phantom"],
                       cwd=wt, check=True, capture_output=True)
        _destroy_worker_worktree(wt)
        after = self._operator_state()
        self.assertEqual(
            before, after,
            "operator repo state must be byte-identical pre and post",
        )

    def test_destroy_handles_already_gone_directory_silently(self):
        wt = _create_worker_worktree(self.tmpdir, "test-spawn-7")
        # Simulate a partial prior cleanup -- directory removed from
        # disk but worktree registration still present (or vice versa).
        # Using `git worktree remove` here would already do both;
        # we need to verify our destroy is idempotent.
        first = _destroy_worker_worktree(wt)
        self.assertEqual(first, [])
        # Calling destroy again on a path that no longer exists must
        # not raise.
        second = _destroy_worker_worktree(wt)
        self.assertEqual(second, [])

    def test_lock_worktree_readonly_blocks_writes(self):
        """After _lock_worktree_readonly, writes inside the worktree
        fail with EACCES. This is what stops the model's freelance
        Edit calls when it misreads a customer prompt as a dev task."""
        wt = _create_worker_worktree(self.tmpdir, "test-lock-1")
        self.addCleanup(_destroy_worker_worktree, wt)

        target = Path(wt, "skills", "module.js")
        self.assertEqual(target.read_text(), "export {};\n")

        _lock_worktree_readonly(wt)

        with self.assertRaises(PermissionError):
            target.write_text("modified\n")
        # Read still works.
        self.assertEqual(target.read_text(), "export {};\n")

    def test_lock_worktree_blocks_new_files_in_existing_dirs(self):
        """Locked dirs lose write permission, so creating new files
        inside them fails. The model's `Write` of a fresh file in
        skills/ is the same shape as Edit on an existing one."""
        wt = _create_worker_worktree(self.tmpdir, "test-lock-2")
        self.addCleanup(_destroy_worker_worktree, wt)
        _lock_worktree_readonly(wt)

        with self.assertRaises(PermissionError):
            Path(wt, "skills", "new-file.js").write_text("// new\n")

    def test_unlock_worktree_restores_writes(self):
        """_unlock_worktree must reverse the lock so teardown can
        delete the directory tree."""
        wt = _create_worker_worktree(self.tmpdir, "test-lock-3")
        # Don't auto-cleanup -- this test verifies cleanup works
        # post-unlock.
        target = Path(wt, "skills", "module.js")
        _lock_worktree_readonly(wt)
        with self.assertRaises(PermissionError):
            target.write_text("modified\n")

        _unlock_worktree(wt)
        target.write_text("modified post-unlock\n")
        self.assertEqual(target.read_text(), "modified post-unlock\n")

        # Destroy still succeeds after lock+unlock+modify cycle.
        failures = _destroy_worker_worktree(wt)
        self.assertEqual(failures, [])
        self.assertFalse(os.path.exists(wt))

    def test_destroy_succeeds_on_locked_worktree(self):
        """If the lock is in place at teardown time, _destroy_worker_
        worktree must still succeed (it calls _unlock_worktree first
        as a defensive measure)."""
        wt = _create_worker_worktree(self.tmpdir, "test-lock-4")
        _lock_worktree_readonly(wt)
        # No explicit unlock -- destroy should handle it.
        failures = _destroy_worker_worktree(wt)
        self.assertEqual(failures, [])
        self.assertFalse(os.path.exists(wt))


class TestSpawnAndBailWorktreeIsolation(unittest.TestCase):
    """End-to-end coverage of the worktree-based _spawn_and_bail.
    Patches run_with_retry_aware_bail with stubs that simulate eval
    behavior inside the worktree. Verifies the spawn cwd is the
    worktree (not the operator repo), contamination inside the
    worktree doesn't propagate, and the bail dict reflects the new
    isolation contract.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test"],
            ["git", "config", "commit.gpgsign", "false"],
        ):
            subprocess.run(cmd, cwd=self.tmpdir, check=True,
                           capture_output=True)
        Path(self.tmpdir, "skills").mkdir()
        self.victim = Path(self.tmpdir, "skills", "module.js")
        self.victim.write_text("export {};\n")
        subprocess.run(
            ["git", "add", "-A"], cwd=self.tmpdir, check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "baseline"],
            cwd=self.tmpdir, check=True, capture_output=True,
        )

    def test_spawn_runs_in_worktree_not_operator_repo(self):
        """The cwd passed to run_with_retry_aware_bail must be the
        worktree path, not the operator repo. This is what makes
        an eval agent's `git checkout -b` (and similar) target the
        ephemeral worktree instead of clobbering operator state."""
        from stream_eval.runner import _spawn_and_bail
        captured_cwds = []

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            captured_cwds.append(cwd)
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail", side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    _spawn_and_bail(
                        "fake query", tf.name, timeout=30, cwd=self.tmpdir,
                    )

        self.assertEqual(len(captured_cwds), 1)
        self.assertNotEqual(
            os.path.realpath(captured_cwds[0]),
            os.path.realpath(self.tmpdir),
            "spawn cwd must NOT be the operator repo",
        )

    def test_spawn_contamination_inside_worktree_does_not_leak(self):
        """The full ugly contamination shape from iteration-baseline
        (branch + clone + tracked-file edit) all happening inside the
        worktree. Operator repo must end byte-identical to start.

        The worktree is now locked read-only before the spawn runs;
        a determined eval agent that bypasses the lock (e.g. via
        `chmod u+w` in Bash, then writes) is the worst case we still
        have to be safe against. This test simulates exactly that:
        chmod the targets writable, then write, then verify the
        operator repo is untouched."""
        from stream_eval.runner import _spawn_and_bail

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            # The eval agent bypasses the lock to enact contamination
            # inside the worktree. A real agent could do this
            # via Bash; the test does it directly for determinism.
            module_js = Path(cwd, "skills", "module.js")
            os.chmod(module_js, 0o644)
            os.chmod(Path(cwd, "skills"), 0o755)
            os.chmod(cwd, 0o755)
            subprocess.run(["git", "checkout", "-b", "feat/eval-phantom"],
                           cwd=cwd, check=True, capture_output=True)
            module_js.write_text("CONTAMINATED\n")
            Path(cwd, "phantom-clone").mkdir()
            Path(cwd, "phantom-clone", "README").write_text("phantom\n")
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        baseline_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        baseline_branches = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)",
             "refs/heads/"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        ).stdout.splitlines()

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail", side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    _spawn_and_bail(
                        "fake query", tf.name, timeout=30, cwd=self.tmpdir,
                    )

        # Operator's tracked file untouched.
        self.assertEqual(self.victim.read_text(), "export {};\n")
        # Operator's HEAD untouched.
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(head_after, baseline_head)
        # Phantom branch did NOT land on the operator repo.
        # (It IS on the operator repo's `branch` listing because
        # worktrees share refs -- we assert it's gone after the
        # worktree was destroyed.)
        branches_after = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)",
             "refs/heads/"],
            cwd=self.tmpdir, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(sorted(branches_after), sorted(baseline_branches),
                         "phantom branch should not persist on operator repo")
        # No phantom-clone leak into operator worktree.
        self.assertFalse(
            Path(self.tmpdir, "phantom-clone").exists(),
            "phantom clone must not appear in operator worktree",
        )

    def test_spawn_clean_run_reports_worktree_uncontaminated(self):
        """A spawn that touches nothing inside the worktree leaves the
        bail dict's contamination flag False."""
        from stream_eval.runner import _spawn_and_bail

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail", side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    bail = _spawn_and_bail(
                        "fake query", tf.name, timeout=30, cwd=self.tmpdir,
                    )

        self.assertFalse(bail["worktree_contaminated"])
        self.assertEqual(bail["worktree_changed_paths"], [])
        self.assertEqual(bail["worktree_restore_failures"], [])

    def test_spawn_contamination_inside_worktree_flags_bail_dict(self):
        """Detection still fires: if the spawn leaves the worktree
        dirty (any shape), bail['worktree_contaminated'] is True. The
        ISOLATION property is that the operator repo is unaffected;
        the DETECTION property is so the eval can mark runs unaudited.
        The lock added in _spawn_and_bail prevents the easy contamination
        shape; this test simulates a determined bypass to verify
        detection still works when the lock is circumvented."""
        from stream_eval.runner import _spawn_and_bail

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            module_js = Path(cwd, "skills", "module.js")
            os.chmod(module_js, 0o644)
            os.chmod(Path(cwd, "skills"), 0o755)
            module_js.write_text("CONTAMINATED\n")
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail", side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    bail = _spawn_and_bail(
                        "fake query", tf.name, timeout=30, cwd=self.tmpdir,
                    )

        self.assertTrue(bail["worktree_contaminated"])
        self.assertIn("skills/module.js", bail["worktree_changed_paths"])

    def test_spawn_locks_worktree_before_spawn_runs(self):
        """End-to-end: _spawn_and_bail wires _lock_worktree_readonly
        between worktree creation and the spawn. A naive write that
        doesn't bypass the lock fails with EACCES, the spawn finishes
        normally, and the operator repo is untouched."""
        from stream_eval.runner import _spawn_and_bail

        write_attempts = []

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            # Attempt the naive contamination shape (no lock bypass).
            try:
                Path(cwd, "skills", "module.js").write_text("naive\n")
                write_attempts.append("succeeded")
            except PermissionError:
                write_attempts.append("blocked")
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail", side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    bail = _spawn_and_bail(
                        "fake query", tf.name, timeout=30, cwd=self.tmpdir,
                    )

        self.assertEqual(
            write_attempts, ["blocked"],
            "naive write into locked worktree must fail with PermissionError",
        )
        self.assertFalse(bail["worktree_contaminated"])
        # Operator file is unchanged.
        self.assertEqual(
            self.victim.read_text(), "export {};\n",
        )

    def test_spawn_operator_wip_outside_worktree_is_invisible(self):
        """The `worktree-at-HEAD` property: uncommitted operator edits
        on tracked files do NOT appear in the worktree. This is what
        keeps the harness from eating its own source mid-eval."""
        from stream_eval.runner import _spawn_and_bail

        # Operator has an uncommitted edit on a tracked file.
        Path(self.tmpdir, "skills", "module.js").write_text(
            "OPERATOR WIP -- harness self-edit\n"
        )

        seen_content = []

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            # The eval reads the worktree's copy of the file.
            seen_content.append(
                Path(cwd, "skills", "module.js").read_text()
            )
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail", side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    _spawn_and_bail(
                        "fake query", tf.name, timeout=30, cwd=self.tmpdir,
                    )

        self.assertEqual(
            seen_content, ["export {};\n"],
            "worktree should show HEAD content, not operator WIP",
        )
        # Operator's WIP edit must STILL be on disk after spawn.
        self.assertEqual(
            Path(self.tmpdir, "skills", "module.js").read_text(),
            "OPERATOR WIP -- harness self-edit\n",
        )

    def test_writable_spawn_captures_artifacts_and_flags_tracked_edits(self):
        from stream_eval.runner import _spawn_and_bail

        def fake_spawn(
            cmd, transcript_path, env, cwd, timeout, classify_event=None,
        ):
            Path(cwd, "generated.txt").write_text("new artifact\n")
            Path(cwd, "skills", "module.js").write_text("changed artifact\n")
            Path(transcript_path).write_text(
                '{"type":"result","result":"ok"}\n'
            )
            return {
                "retry_budget_exhausted": False,
                "wall_timed_out": False,
                "total_retries": 0,
                "latest_attempt": 0,
                "max_retries_field": 0,
                "exit_code": 0,
            }

        with mock.patch.dict(os.environ, {"STREAM_EVAL_PROFILE": "inherit"}):
            with mock.patch(
                "stream_eval.runner.run_with_retry_aware_bail",
                side_effect=fake_spawn,
            ):
                with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
                    bail = _spawn_and_bail(
                        "fake query",
                        tf.name,
                        timeout=30,
                        cwd=self.tmpdir,
                        allow_worktree_writes=True,
                    )

        artifacts = {item["path"]: item["content"] for item in bail["artifacts"]}
        self.assertEqual(artifacts["generated.txt"], "new artifact\n")
        self.assertEqual(
            artifacts["skills/module.js"],
            "changed artifact\n",
        )
        self.assertTrue(bail["worktree_contaminated"])
        self.assertEqual(
            bail["worktree_changed_paths"],
            ["skills/module.js"],
        )


class TestResolveHarnessVersion(unittest.TestCase):
    """_resolve_harness_version reads .git/HEAD when available, falls
    back to stream_eval.__version__, returns ('unknown', 'unknown') if
    neither lookup works."""

    def test_resolves_from_git_head_branch_ref(self):
        """When the package lives in a git repo whose HEAD points at a
        branch ref, _resolve_harness_version returns the resolved SHA."""
        from stream_eval import runner

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            git_dir = tmp / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
            (git_dir / "refs" / "heads").mkdir(parents=True)
            (git_dir / "refs" / "heads" / "main").write_text(
                "abc1234567890abcdef1234567890abcdef123456\n"
            )

            fake_pkg = tmp / "stream_eval"
            fake_pkg.mkdir()
            (fake_pkg / "__init__.py").write_text("__version__ = '9.9.9'\n")

            version, kind = runner._resolve_harness_version(package_dir=fake_pkg)
            self.assertEqual(version, "abc1234567890abcdef1234567890abcdef123456")
            self.assertEqual(kind, "git_sha")

    def test_resolves_from_detached_head(self):
        """When HEAD contains a SHA directly (detached HEAD), use it."""
        from stream_eval import runner

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            git_dir = tmp / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text(
                "abc1234567890abcdef1234567890abcdef123456\n"
            )

            fake_pkg = tmp / "stream_eval"
            fake_pkg.mkdir()
            (fake_pkg / "__init__.py").write_text("__version__ = '9.9.9'\n")

            version, kind = runner._resolve_harness_version(package_dir=fake_pkg)
            self.assertEqual(version, "abc1234567890abcdef1234567890abcdef123456")
            self.assertEqual(kind, "git_sha")

    def test_resolves_from_git_submodule_gitfile(self):
        """When stream-eval is consumed as a git submodule, the
        package's parent has a .git FILE (not a directory) containing
        `gitdir: ../path/to/real-git-dir`. The resolver must follow
        that indirection. Without this, every submodule consumer's
        results.json stamps the package version (`0.1.0`) instead of
        the pinned SHA, breaking iteration-to-commit correlation."""
        from stream_eval import runner

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # The real .git/modules/<name>/ directory: this is where
            # the parent repo stashes a submodule's metadata.
            real_git = tmp / "parent" / ".git" / "modules" / "harness"
            real_git.mkdir(parents=True)
            (real_git / "HEAD").write_text("ref: refs/heads/main\n")
            (real_git / "refs" / "heads").mkdir(parents=True)
            (real_git / "refs" / "heads" / "main").write_text(
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
            )

            # The submodule checkout: package's parent has a .git
            # FILE pointing at ../parent/.git/modules/harness via
            # the relative path that submodules actually use.
            sub_root = tmp / "parent" / "harness"
            sub_root.mkdir()
            (sub_root / ".git").write_text(
                "gitdir: ../.git/modules/harness\n"
            )

            fake_pkg = sub_root / "stream_eval"
            fake_pkg.mkdir()
            (fake_pkg / "__init__.py").write_text("__version__ = '0.1.0'\n")

            version, kind = runner._resolve_harness_version(package_dir=fake_pkg)
            self.assertEqual(version, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
            self.assertEqual(kind, "git_sha")

    def test_falls_back_to_package_version(self):
        """No .git directory anywhere -> fall back to __version__."""
        from stream_eval import runner

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake_pkg = tmp / "stream_eval"
            fake_pkg.mkdir()
            (fake_pkg / "__init__.py").write_text("__version__ = '0.5.7'\n")

            version, kind = runner._resolve_harness_version(package_dir=fake_pkg)
            self.assertEqual(version, "0.5.7")
            self.assertEqual(kind, "package_version")

    def test_unknown_when_neither_works(self):
        """Missing .git AND missing __version__ attr -> 'unknown'."""
        from stream_eval import runner

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake_pkg = tmp / "stream_eval"
            fake_pkg.mkdir()
            (fake_pkg / "__init__.py").write_text("# no version\n")

            version, kind = runner._resolve_harness_version(package_dir=fake_pkg)
            self.assertEqual(version, "unknown")
            self.assertEqual(kind, "unknown")


class TestRunEvalEnvelope(unittest.TestCase):
    """The results envelope must include harness_version and
    harness_version_kind fields so iteration notes can correlate
    numbers to a specific harness commit."""

    def test_run_eval_writes_harness_version_to_results(self):
        """Run an empty fixture set so no real agent CLI is invoked.
        The envelope is built regardless of whether any tasks ran."""
        from stream_eval import runner

        results, exit_code = runner.run_eval(
            kind="trigger",
            fixtures=[],
            get_fixture_id=lambda fx: fx.get("name"),
            get_query=lambda fx: fx.get("query", ""),
            score_run=lambda fx, tp, b: (True, {}),
            summarize=lambda x: [],
            runs_per_fixture=1,
            workers=1,
            timeout=10,
            cwd=os.getcwd(),
            transcript_dir=None,
            summary_label="queries",
            skill_name="testskill",
            eval_path="dummy.json",
        )
        self.assertIn("harness_version", results)
        self.assertIn("harness_version_kind", results)
        self.assertIn(
            results["harness_version_kind"],
            ("git_sha", "package_version", "unknown"),
        )


class TestPerFixtureWallClockSkip(unittest.TestCase):
    """wall_clock-without-retry on a single fixture skips that fixture's
    remaining runs but does NOT abort the eval. Other fixtures keep
    going. Distinguishes "this prompt makes the model think too long"
    from "the upstream is poisoned" (retry_budget_exhausted)."""

    def test_wall_clock_skips_fixture_continues_eval(self):
        """3 fixtures x 2 runs = 6 tasks. Fixture beta times out on its
        first run (wall_clock). beta-2 must be cancelled but alpha and
        gamma's full sets must still run."""
        from stream_eval import runner

        fixtures = [
            {"name": "alpha", "q": "qa"},
            {"name": "beta", "q": "qb"},
            {"name": "gamma", "q": "qc"},
        ]
        scored_calls = []

        def fake_runner(fixture, run_idx, fixture_id, transcript_dir,
                        timeout, cwd, get_query, score_run,
                        skill_path=None, also_install=()):
            scored_calls.append((fixture_id, run_idx))
            # beta's first run wall-clocks; everything else returns
            # cleanly. Crucially, we don't abort.
            beta_first = (fixture_id == "beta" and run_idx == 1)
            return {
                "fixture_id": fixture_id, "run_idx": run_idx,
                "elapsed_seconds": 0.01, "total_retries": 0,
                "timed_out": beta_first,
                "timeout_reason": "wall_clock" if beta_first else None,
                "transcript_path": None,
                "pass_": not beta_first, "kind_extra": {},
            }

        with tempfile.TemporaryDirectory() as td:
            with mock.patch(
                "stream_eval.runner._run_one_task", side_effect=fake_runner,
            ):
                results, exit_code = runner.run_eval(
                    kind="trigger",
                    fixtures=fixtures,
                    get_fixture_id=lambda fx: fx.get("name"),
                    get_query=lambda fx: fx["q"],
                    score_run=None,
                    summarize=lambda fws: [
                        {"fixture_id": f["fixture_id"]} for f in fws
                    ],
                    runs_per_fixture=2, workers=1, timeout=10,
                    cwd=str(td),
                    transcript_dir=None,
                    summary_label="queries",
                    skill_name="test-skill",
                    eval_path="evals/test/trigger-eval.json",
                    executor_class=ThreadPoolExecutor,
                )

        # Eval should NOT abort.
        self.assertFalse(
            results.get("aborted_on_timeout"),
            "wall_clock on a single fixture must not abort the eval",
        )
        self.assertNotEqual(
            exit_code, 3,
            "wall_clock-skip path should not return exit code 3",
        )

        # Scored calls: every fixture's run 1 (round-robin), then
        # every fixture's run 2 EXCEPT beta-2 (skipped).
        # Round-robin order is run-major: r1 for all fixtures, then r2.
        called_pairs = set(scored_calls)
        self.assertIn(("alpha", 1), called_pairs)
        self.assertIn(("alpha", 2), called_pairs)
        self.assertIn(("beta", 1), called_pairs)
        self.assertNotIn(
            ("beta", 2), called_pairs,
            "beta-2 must be skipped after beta-1 wall-clock-timed-out",
        )
        self.assertIn(("gamma", 1), called_pairs)
        self.assertIn(("gamma", 2), called_pairs)

        # Envelope tracks the skip.
        self.assertEqual(results.get("skipped_runs_for_fixture_timeout"), 1)
        self.assertEqual(results.get("skipped_fixtures"), ["beta"])
        # Done count includes the skipped run, so total_runs_planned is
        # also reached.
        self.assertEqual(
            results["completed_runs"], results["total_runs_planned"],
            "completed_runs should equal planned (skipped runs counted)",
        )

    def test_wall_clock_in_retry_still_aborts_eval(self):
        """wall_clock_in_retry is an upstream-poisoned signal (the CLI
        is wedged inside a retry sleep). That keeps the abort-eval
        policy -- only plain wall_clock gets the per-fixture skip."""
        from stream_eval import runner

        fixtures = [{"name": "alpha", "q": "qa"}, {"name": "beta", "q": "qb"}]

        def fake_runner(fixture, run_idx, fixture_id, transcript_dir,
                        timeout, cwd, get_query, score_run,
                        skill_path=None, also_install=()):
            return {
                "fixture_id": fixture_id, "run_idx": run_idx,
                "elapsed_seconds": 0.01, "total_retries": 5,
                "timed_out": True,
                "timeout_reason": "wall_clock_in_retry",
                "transcript_path": None,
                "pass_": False, "kind_extra": {},
            }

        with tempfile.TemporaryDirectory() as td:
            with mock.patch(
                "stream_eval.runner._run_one_task", side_effect=fake_runner,
            ):
                results, exit_code = runner.run_eval(
                    kind="trigger",
                    fixtures=fixtures,
                    get_fixture_id=lambda fx: fx.get("name"),
                    get_query=lambda fx: fx["q"],
                    score_run=None,
                    summarize=lambda fws: [],
                    runs_per_fixture=1, workers=1, timeout=10,
                    cwd=str(td),
                    transcript_dir=None,
                    summary_label="queries",
                    skill_name="test-skill",
                    eval_path="evals/test/trigger-eval.json",
                    executor_class=ThreadPoolExecutor,
                )

        self.assertTrue(
            results.get("aborted_on_timeout"),
            "wall_clock_in_retry must still abort the whole eval",
        )
        self.assertEqual(exit_code, 3)


class TestOperatorStop(unittest.TestCase):
    """The dashboard's stop button calls dispatcher.stop() externally.
    The runner's main loop must notice and exit even though
    `done < total` -- otherwise the run hangs forever waiting on
    results that will never come (no spawns, queue drained)."""

    def test_external_dispatcher_stop_exits_main_loop(self):
        """Simulate the operator clicking 'stop' mid-run:
        target_workers goes to 0 and dispatcher.state goes to STOPPED.
        The main loop must exit and tag the run aborted."""
        from stream_eval import runner

        fixtures = [
            {"name": "alpha", "q": "qa"},
            {"name": "beta", "q": "qb"},
            {"name": "gamma", "q": "qc"},
        ]
        gate = threading.Event()

        def fake_runner(fixture, run_idx, fixture_id, transcript_dir,
                        timeout, cwd, get_query, score_run,
                        skill_path=None, also_install=()):
            """alpha-1 runs immediately, then signals the test thread
            to stop the dispatcher. Subsequent calls block on a gate
            (released at end-of-test) so they're cancellable but don't
            hang the test if cancellation is broken."""
            is_first = (fixture_id == "alpha" and run_idx == 1)
            if not is_first:
                gate.wait(timeout=2.0)
            return {
                "fixture_id": fixture_id, "run_idx": run_idx,
                "elapsed_seconds": 0.01, "total_retries": 0,
                "timed_out": False, "timeout_reason": None,
                "transcript_path": None, "pass_": True, "kind_extra": {},
            }

        # Stop the dispatcher 0.1s into the run, after alpha-1 has
        # completed. Use the module-global hook the control surfaces
        # already use.
        def stop_after_delay():
            time.sleep(0.1)
            d = runner.get_current_dispatcher()
            if d is not None:
                d.target_workers = 0
                d.stop()

        stopper = threading.Thread(target=stop_after_delay, daemon=True)
        stopper.start()

        try:
            with tempfile.TemporaryDirectory() as td:
                with mock.patch(
                    "stream_eval.runner._run_one_task",
                    side_effect=fake_runner,
                ):
                    results, exit_code = runner.run_eval(
                        kind="trigger",
                        fixtures=fixtures,
                        get_fixture_id=lambda fx: fx.get("name"),
                        get_query=lambda fx: fx["q"],
                        score_run=None,
                        summarize=lambda fws: [
                            {"fixture_id": f["fixture_id"]} for f in fws
                        ],
                        runs_per_fixture=2, workers=1, timeout=10,
                        cwd=str(td),
                        transcript_dir=None,
                        summary_label="queries",
                        skill_name="test-skill",
                        eval_path="evals/test/trigger-eval.json",
                        executor_class=ThreadPoolExecutor,
                    )
        finally:
            gate.set()
            stopper.join(timeout=1.0)

        # Operator-stopped run produces an envelope, returns exit 1
        # (failure-ish but not poisoned), is tagged operator_stopped,
        # is NOT aborted_on_timeout (separate signal).
        self.assertEqual(exit_code, 1)
        self.assertTrue(results.get("operator_stopped"))
        self.assertFalse(results.get("aborted_on_timeout"))
        self.assertLess(
            results["completed_runs"], results["total_runs_planned"],
            "operator stop should cut the run short",
        )


class TestRunOneTaskTimedOutScoring(unittest.TestCase):
    """_run_one_task scores even on timed-out runs so kind_extra
    (first_tool, first_skill, assertion_results) survives partial runs.
    Without this, callers have to re-read transcripts to recover the
    trigger signal, defeating the harness's progress line and
    results.json shape."""

    def _run_with_mocked_spawn(self, *, timed_out, score_fn):
        """Helper: invoke _run_one_task with _spawn_and_bail mocked to
        return a synthetic bail dict. score_fn is called by the runner."""
        import tempfile
        from unittest import mock
        from stream_eval import runner

        bail = {
            "retry_budget_exhausted": False,
            "wall_timed_out": timed_out,
            "wall_timed_out_in_retry": False,
            "total_retries": 0,
            "latest_attempt": 0,
            "max_retries_field": 0,
            "time_in_retries": 0.0,
            "exit_code": 0 if not timed_out else None,
            "worktree_contaminated": False,
            "worktree_changed_paths": [],
            "worktree_restore_failures": [],
        }

        with tempfile.TemporaryDirectory() as td:
            with mock.patch(
                "stream_eval.runner._spawn_and_bail", return_value=bail,
            ):
                return runner._run_one_task(
                    fixture={"name": "fx1", "query": "test"},
                    run_idx=1,
                    fixture_id="fx1",
                    transcript_dir=td,
                    timeout=10,
                    cwd=td,
                    get_query=lambda fx: fx["query"],
                    score_run=score_fn,
                )

    def test_timed_out_run_still_calls_score_run(self):
        """A wall-clock timeout doesn't bypass scoring -- kind_extra
        must be populated from the partial transcript."""
        score_calls = []

        def score(fixture, transcript_path, bail):
            score_calls.append(fixture["name"])
            return True, {"first_tool": "Skill", "first_skill": "x"}

        result = self._run_with_mocked_spawn(timed_out=True, score_fn=score)

        self.assertEqual(score_calls, ["fx1"], "score_run must be called")
        self.assertTrue(result["timed_out"])
        self.assertFalse(
            result["pass_"], "timed-out run is forced fail regardless of score",
        )
        self.assertEqual(
            result["kind_extra"],
            {"first_tool": "Skill", "first_skill": "x"},
            "kind_extra must reflect what score_run returned",
        )

    def test_clean_run_pass_unchanged_by_timeout_logic(self):
        """Non-timeout run: score_run's pass verdict flows through."""
        def score(fixture, transcript_path, bail):
            return True, {"first_tool": "Skill", "first_skill": "y"}

        result = self._run_with_mocked_spawn(timed_out=False, score_fn=score)

        self.assertFalse(result["timed_out"])
        self.assertTrue(result["pass_"])
        self.assertEqual(result["kind_extra"]["first_skill"], "y")

    def test_score_run_raising_does_not_crash_runner(self):
        """If score_run errors on a partial transcript (e.g. malformed
        JSONL after truncation), the runner must not propagate -- a
        broken scorer per-run shouldn't kill the whole eval. pass_ is
        False, kind_extra is empty."""
        def score(fixture, transcript_path, bail):
            raise ValueError("partial transcript")

        result = self._run_with_mocked_spawn(timed_out=True, score_fn=score)

        self.assertFalse(result["pass_"])
        self.assertEqual(result["kind_extra"], {})


if __name__ == "__main__":
    unittest.main()

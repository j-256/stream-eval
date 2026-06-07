"""Unit tests for stream_eval/trigger.py.

Run with: pytest tests/test_trigger.py
"""
import json
import tempfile
import unittest
from pathlib import Path

import stream_eval.trigger as trigger_eval
score_trigger_run = trigger_eval.score_trigger_run


class TestTranscriptDirFor(unittest.TestCase):
    def test_namespaces_by_out_stem(self):
        cold = trigger_eval.transcript_dir_for(
            Path("/tmp/iter-x/results-cold.json")
        )
        warm = trigger_eval.transcript_dir_for(
            Path("/tmp/iter-x/results-warm.json")
        )
        self.assertEqual(cold, Path("/tmp/iter-x/transcripts/results-cold"))
        self.assertEqual(warm, Path("/tmp/iter-x/transcripts/results-warm"))
        self.assertNotEqual(cold, warm)


class TestScoreTriggerRun(unittest.TestCase):
    """Replaces TestRunOne.

    score_trigger_run is the trigger-eval scoring callback the runner
    invokes per (fixture, run). It receives a fixture, a transcript
    path, and the bail dict; returns (pass: bool, kind_extra: dict).
    """

    def _write_transcript(self, td, lines):
        path = Path(td) / "fake-transcript.jsonl"
        path.write_text("".join(json.dumps(L) + "\n" for L in lines))
        return path

    def test_pass_when_correct_skill_fires(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = self._write_transcript(td, [
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "dsc-triage"}},
                ]}},
            ])
            fixture = {"query": "q", "should_trigger": True}
            bail = {"retry_budget_exhausted": False, "wall_timed_out": False}
            pass_, extra = score_trigger_run(
                fixture, str(transcript), bail,
                target_skill="dsc-triage",
            )
            self.assertTrue(pass_)
            self.assertTrue(extra["triggered"])
            self.assertEqual(extra["first_tool"], "Skill")
            self.assertEqual(extra["first_skill"], "dsc-triage")

    def test_fail_when_wrong_skill_fires(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = self._write_transcript(td, [
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "wrong-skill"}},
                ]}},
            ])
            fixture = {"query": "q", "should_trigger": True}
            bail = {"retry_budget_exhausted": False, "wall_timed_out": False}
            pass_, extra = score_trigger_run(
                fixture, str(transcript), bail,
                target_skill="dsc-triage",
            )
            self.assertFalse(pass_)
            self.assertFalse(extra["triggered"])
            self.assertEqual(extra["first_skill"], "wrong-skill")

    def test_fail_when_non_skill_tool_fires(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = self._write_transcript(td, [
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ]}},
            ])
            fixture = {"query": "q", "should_trigger": True}
            bail = {"retry_budget_exhausted": False, "wall_timed_out": False}
            pass_, extra = score_trigger_run(
                fixture, str(transcript), bail,
                target_skill="dsc-triage",
            )
            self.assertFalse(pass_)
            self.assertEqual(extra["first_tool"], "Bash")

    def test_pass_when_no_tool_used_and_should_not_trigger(self):
        """A single run that correctly didn't trigger reports pass_=True
        because the per-run pass means 'matched expected.' On a fixture
        with should_trigger=False, not-firing IS the correct outcome --
        so the per-run cell renders green on the dashboard. kind_extra
        carries triggered=False (the underlying signal) for downstream
        consumers like summarize() that compute the trigger rate."""
        with tempfile.TemporaryDirectory() as td:
            transcript = self._write_transcript(td, [
                {"type": "result", "result": "text-only answer"},
            ])
            fixture = {"query": "q", "should_trigger": False}
            bail = {"retry_budget_exhausted": False, "wall_timed_out": False}
            pass_, extra = score_trigger_run(
                fixture, str(transcript), bail,
                target_skill="dsc-triage",
            )
            self.assertTrue(pass_, "correct decline should pass")
            self.assertFalse(extra["triggered"])
            self.assertIsNone(extra["first_tool"])

    def test_fail_when_should_not_trigger_but_did(self):
        """Inverse of test_pass_when_no_tool_used_and_should_not_trigger:
        if a fixture is should_trigger=False but the skill DID fire,
        the run is incorrect -- pass_=False, triggered=True."""
        with tempfile.TemporaryDirectory() as td:
            transcript = self._write_transcript(td, [{
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Skill",
                     "input": {"skill": "dsc-triage"}},
                ]},
            }])
            fixture = {"query": "q", "should_trigger": False}
            bail = {"retry_budget_exhausted": False, "wall_timed_out": False}
            pass_, extra = score_trigger_run(
                fixture, str(transcript), bail,
                target_skill="dsc-triage",
            )
            self.assertFalse(pass_, "over-fire on a decline fixture should fail")
            self.assertTrue(extra["triggered"])


class TestPreflightGuards(unittest.TestCase):
    """`stream-eval trigger --profile=isolated` (the default) without
    --skill-path must fail at the CLI, not in every spawned worker."""

    def test_profile_isolated_without_skill_path_errors(self):
        # ap.error calls SystemExit(2). We don't need a real eval file
        # because argparse fails before reading any.
        with self.assertRaises(SystemExit) as ctx:
            trigger_eval.main([
                "--eval", "/tmp/nonexistent.json",
                "--out", "/tmp/nonexistent-out.json",
            ])
        self.assertEqual(ctx.exception.code, 2)


class TestValidateFixtures(unittest.TestCase):
    """Trigger fixtures must validate before any runs spawn -- mirrors
    stream_eval.synthesis's exit-code-2 semantics. Without validation,
    a missing field would only surface mid-run as a confusing
    KeyError. Failure here returns exit 2 cleanly."""

    def test_valid_fixtures_pass(self):
        fixtures = [
            {"query": "anything", "should_trigger": True},
            {"name": "decline", "query": "x", "should_trigger": False},
        ]
        trigger_eval.validate_fixtures(fixtures)  # should not raise

    def test_top_level_must_be_list(self):
        with self.assertRaises(trigger_eval.FixtureSchemaError):
            trigger_eval.validate_fixtures({"query": "x", "should_trigger": True})

    def test_missing_query_raises(self):
        fixtures = [{"should_trigger": True}]
        with self.assertRaises(trigger_eval.FixtureSchemaError):
            trigger_eval.validate_fixtures(fixtures)

    def test_empty_query_raises(self):
        fixtures = [{"query": "", "should_trigger": True}]
        with self.assertRaises(trigger_eval.FixtureSchemaError):
            trigger_eval.validate_fixtures(fixtures)

    def test_missing_should_trigger_raises(self):
        fixtures = [{"query": "x"}]
        with self.assertRaises(trigger_eval.FixtureSchemaError):
            trigger_eval.validate_fixtures(fixtures)

    def test_should_trigger_must_be_bool(self):
        # The string "true" is a common mistake; explicitly reject it
        # so the author gets a clear error rather than silently treating
        # truthy strings as True.
        fixtures = [{"query": "x", "should_trigger": "true"}]
        with self.assertRaises(trigger_eval.FixtureSchemaError):
            trigger_eval.validate_fixtures(fixtures)

    def test_duplicate_names_raise(self):
        fixtures = [
            {"name": "dup", "query": "x", "should_trigger": True},
            {"name": "dup", "query": "y", "should_trigger": False},
        ]
        with self.assertRaises(trigger_eval.FixtureSchemaError):
            trigger_eval.validate_fixtures(fixtures)

    def test_name_optional_when_absent(self):
        # Most authored fixtures omit name; the runner assigns qN. The
        # validator must allow that, only enforcing uniqueness when name
        # is present.
        fixtures = [
            {"query": "x", "should_trigger": True},
            {"query": "y", "should_trigger": False},
        ]
        trigger_eval.validate_fixtures(fixtures)

    def test_empty_name_string_raises(self):
        # If name is provided, it must be a non-empty string. An empty
        # string is almost certainly an authoring mistake (and would
        # collide silently across fixtures with the same empty name).
        fixtures = [{"name": "", "query": "x", "should_trigger": True}]
        with self.assertRaises(trigger_eval.FixtureSchemaError):
            trigger_eval.validate_fixtures(fixtures)


if __name__ == "__main__":
    unittest.main()

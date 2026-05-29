# Comparison with `skill-creator`

The `skill-creator` plugin (bundled with Claude Code) ships its own evaluation tooling. This doc explains how it differs from stream-eval and what we tested to validate the choice of building a separate harness. Short version:

- `skill-creator`'s **outer workflow** (the SKILL.md-driven flow) is a per-skill, human-in-the-loop design tool. It tests skills end-to-end via real subagents and a browser-based qualitative review. stream-eval doesn't replace it; the two tools answer different questions.
- `skill-creator`'s **`scripts/run_eval.py`** is a narrowly-scoped trigger-eval used by the description-optimization sub-flow. stream-eval's `trigger` command covers the same ground with safer defaults and is wrapped in additional tooling (synthesis evals, dashboard, contamination detection, throttle protocol) that `run_eval.py` doesn't address.

The rest of this doc covers what we tested, what we measured, and what we concluded.

## What `skill-creator` does

`skill-creator/SKILL.md` lays out a workflow with these phases:

1. **Capture intent / interview** – the user describes what the skill should do; the model interviews them about edge cases, formats, dependencies.
2. **Draft the SKILL.md** – name, description, body, optional bundled scripts/references/assets.
3. **Test cases via real subagents** – for each test prompt, spawn two subagents (one with the skill, one without/baseline). Save outputs.
4. **Browser review** – `eval-viewer/generate_review.py` renders an HTML page where the user clicks through outputs, leaves feedback, and sees a quantitative benchmark across configurations.
5. **Iterate** – read feedback, improve the skill, rerun.
6. **Description optimization** (optional, late stage) – `scripts/run_loop.py` iteratively rewrites the skill's frontmatter description, evaluating each candidate against a held-out trigger-eval set via `scripts/run_eval.py`.

The first five phases are the bulk of skill-creator. Phase 6 is where `run_eval.py` lives, and it's the only piece of skill-creator with semantics directly comparable to anything in stream-eval.

## How `run_eval.py` works

For each query in the eval set, the harness:

1. Writes a slash-command file at `<project-root>/.claude/commands/<skill-name>-skill-<uuid>.md`. The frontmatter description is the skill's description (read from the `--skill-path` SKILL.md). The body is non-functional: `# <skill-name>\n\nThis skill handles: <description>`.
2. Runs `claude -p '<query>' --output-format stream-json` with `cwd=<project-root>`.
3. Watches the stream and counts the query as a trigger when the first `tool_use` block is for the `Skill` (or `Read`) tool AND the UUID-suffixed clean name appears anywhere in the tool's input. Exits as soon as the match fires – the synthetic itself is never executed.

The skill body, scripts, and references are never exercised; only the description matters to the measurement.

## What we tested

Three experiments, all against `claude-sonnet-4-6` via `claude -p`:

### Experiment 1: stream-eval vs `run_eval.py`, fork-and-pr fixture set, real skill installed

Same fixture set (`evals/fork-and-pr/trigger-eval.json`, 20 queries, 3 runs each), same model, same skill (`fork-and-pr` symlinked into `~/.claude/skills/`).

| Harness | Pass rate |
|---|---|
| `stream-eval trigger --profile=inherit` | 20/20 |
| `run_eval.py` (real skill installed) | 10/20 |

`run_eval.py`'s failure shape: every positive query at 0/3 triggers, every negative query at 0/3 triggers. The 10/20 score comes entirely from negatives "passing" by not firing on anything.

### Experiment 2: same as 1, but with the real skill uninstalled

To test the hypothesis that the real install was shadowing the synthetic, we removed `~/.claude/skills/fork-and-pr` and re-ran `run_eval.py` against the same fixtures.

| Harness | Pass rate |
|---|---|
| `run_eval.py` (no real install) | 10/20 |

Pass rate didn't change. The shape did: most positives at 0/3, several at 1/3 – the synthetic was sometimes selected, but never enough to flip the per-query verdict at the 0.5 threshold.

### Experiment 3: same harness, different skill (`dsc-scrape`)

To test whether the result was specific to fork-and-pr's description.

| Harness | Pass rate |
|---|---|
| `run_eval.py` against dsc-scrape | 10/20 |

Same shape as Experiment 1: all positives at 0/3, all negatives at 0/3.

### Mechanism probes

Two direct experiments to understand why:

**Catalog probe.** With a synthetic written and the real skill uninstalled, we asked `claude -p` to dump the contents of `available_skills`. The synthetic appeared in the catalog under its UUID-suffixed name (`fork-and-pr-skill-<uuid>`). When the real skill was also installed, both appeared – the canonical entry (`fork-and-pr`) and the synthetic.

**Single-query stream capture.** With the real skill uninstalled and the synthetic in place, we ran two queries and inspected the resulting `tool_use` events:

- `"what's the standard fork-and-PR flow on GitHub?"` – zero tool invocations. The model just answered the question textually.
- `"I want to make a PR to SalesforceCommerceCloud/plugin_sitemap..."` – one tool invocation: `Skill(skill="fork-and-pr-skill-<uuid>")`. The synthetic was correctly selected.

With the real skill installed, the same second query produced `Skill(skill="fork-and-pr")` – the canonical entry, no UUID suffix.

## What we concluded

Two findings we hold confidently, plus an unresolved third:

1. **`run_eval.py`'s premise is correct.** Slash commands at `<project>/.claude/commands/<name>.md` do appear in the model's `Skill`-tool catalog under their UUID-suffixed names. The harness is asking a real question.

2. **It has a shadowing footgun.** When a real skill is installed at `~/.claude/skills/<canonical-name>/` AND `run_eval.py` writes a synthetic `<canonical-name>-skill-<uuid>` for the same skill, both appear in the catalog and the model preferentially invokes the canonical-named entry (one direct stream capture confirms this; consistent with the broader Experiment-1 data). The harness scores that as a miss because the input has no UUID suffix. The footgun bites whenever you happen to have the skill under test installed under its canonical name during the eval run – which can easily happen if you're also using the skill interactively, or if you install via `ln -s skills/<name> ~/.claude/skills/<name>` to dogfood it.

3. **What's not fully explained:** Experiment 2 removed the shadowing real install, leaving only the synthetic visible. We expected `run_eval.py`'s pass rate to climb. It didn't – it stayed at 10/20, with positives shifting from 0/3 to 1/3 on some queries but never enough to flip the 0.5 threshold. So shadowing is a real factor but isn't the whole story. We don't know why; we didn't run further experiments to isolate it. The shadowing case alone was sufficient to motivate stream-eval's profile defaults, and pinning down the rest didn't change our decision.

For comparison, stream-eval's 20/20 result on the same fixtures with `--profile=inherit` was achieved with 29 of 30 positive runs producing `Skill(skill="fork-and-pr")` as the first tool invocation – the canonical triggering path, observed directly via the per-run `kind_extra` data in results.json. So stream-eval's high score isn't an artifact of generous scoring; the model genuinely invoked the installed skill on nearly every positive query.

## How stream-eval differs

For trigger-eval specifically:

- **Profile defaults that avoid the shadowing footgun.** `--profile=isolated` (the default) builds a temp HOME with only `--skill-path skills/<name>` exposed. There's no canonical-named skill to shadow the synthetic.
- **Path-based input.** `--skill-path skills/<name>` reads the canonical name from `SKILL.md`'s frontmatter. The same path the operator has open in their editor is the one being evaluated.

For everything else:

- **Synthesis evals.** `stream-eval synthesis` asserts against the full transcript: tool sequence, citation patterns, prose-rule violations, response shape. `run_eval.py` doesn't do this; `skill-creator`'s outer flow does it qualitatively via human review.
- **Live dashboard.** Per-row worker controls (+1/-1/pause/resume), retry counters, status state machine, in-flight cell visualization. `run_eval.py` runs to completion silently; `skill-creator`'s outer flow has the eval-viewer (a different shape entirely).
- **Worktree contamination detection.** Per-spawn git-state snapshot/restore. Catches eval-Sonnet making file edits during a run.
- **Bail-on-throttle protocol.** Exit code 3 when the upstream API poisons; per-run transcripts up to the abort point are retained.
- **`harness_version` stamped in every results.json.** Iteration-to-commit correlation across many runs, useful for regression triage.

## When to reach for which

stream-eval is built for a particular shape of work: you have an installable skill, you want to iterate on it under realistic conditions, you're doing this often enough that operational tooling (dashboard, contamination detection, throttle handling) starts to matter. If that fits, it's the right tool. The two profile defaults (`isolated` for clean trigger-eval, `inherit` to test against your real environment) cover the common cases.

The other tools are for cases stream-eval doesn't address:

- **`skill-creator`'s outer SKILL.md flow** when you're designing a skill from intent, want browser-based human review of outputs, and you're working on one skill at a time. stream-eval can't replace this; the human-in-the-loop review is the point.
- **`run_eval.py`** ships as part of `skill-creator`'s description-optimization sub-flow (driven by `run_loop.py`). Whether that sub-flow produces useful description improvements in practice is a question for `skill-creator`'s own documentation, not ours – we tested only the underlying measurement, not the optimization loop on top of it. If you're already using `skill-creator` to design a skill, follow its docs for that piece.

## Caveats

Two version dependencies to keep in mind:

- **Claude Code version** governs how slash commands at `<project>/.claude/commands/<name>.md` are surfaced (Conclusion 1's catalog finding). Unlikely to change on the timescale this doc is useful, but if a future Claude Code version changes the surfacing rules, the catalog observation could become stale.
- **Model version** governs the routing preferences in Conclusion 2 (canonical-over-synthetic when both visible) and the skill-invocation heuristics broadly. We tested `claude-sonnet-4-6` only. Other models could route differently.

All three `run_eval.py` runs landed at the same pass rate, with positives at 0/3 (or occasionally 1/3) triggers. Different fixture sets or other models could produce different numbers without contradicting the shadowing finding (Conclusion 2), which we hold confidently within the tested model.

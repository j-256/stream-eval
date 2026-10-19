# Repository instructions

## Read first

Read `README.md` for the evaluation model and `CONTRIBUTING.md` for branch, test, commit, and release conventions before changing behavior.

stream-eval is a measurement harness. Preserve reproducibility and comparability across agents even when a backend-specific shortcut would be easier.

## Branch and publication model

- In a hosted clone, create topic branches from `source` and target contributions back to `source`.
- Treat hosted `main` as a generated distribution tip that may be replaced after publication. Do not author on it or merge it into neutral source history.
- Keep source content host-agnostic. Preserve the `https://github.com/j-256/stream-eval.git` placeholder; the publication workflow supplies the concrete clone URL.
- Before release work, confirm that the checkout represents the neutral source history described in `CONTRIBUTING.md`.
- Publication and release operations mutate remote state. Do not run them without explicit authorization, and use the repository's owning publication workflow rather than direct pushes.

## Preserve the harness contracts

- Keep responsibilities separated: the runner owns orchestration and disposable worktrees, adapters own native invocation and event classification, transcript normalization owns portable actions, and scoring owns fixture outcomes.
- Keep backend-specific behavior behind the adapter boundary, including executable flags, isolated skill locations, nested-session environment cleanup, native event parsing, and retry telemetry.
- Treat normalized actions as the cross-agent contract. Preserve native events for backend-specific assertions, but do not leak their shapes into portable assertions.
- Keep Codex activation inference narrow: only a read of the known target or expected skill's `SKILL.md` counts. Reading an unrelated skill is not activation.
- Preserve `agent`, `model`, and harness version as measurement dimensions. Do not merge results from different dimensions into one baseline without separating them.

## Isolation and artifacts

- Keep `isolated` reproducible and `restricted` integration-free. Do not expose user configuration, MCP servers, plugins, or subagents through either profile; `inherit` is the explicit diagnostic escape hatch.
- Trigger worktrees remain read-only. Synthesis may capture writes from its disposable worktree, explicit native payloads, and approved system temporary paths only.
- Never read an adapter-reported artifact outside the disposable worktree or system temporary directory. Preserve size limits and path validation when changing artifact collection.
- A tracked-file change is evidence of worktree contamination, not an ordinary artifact. Keep capturing it for diagnosis while marking the run contaminated.

## Fixtures and verification

- Prefer portable assertions. Use native assertions only for an intentionally agent-specific measurement, and label that constraint in the fixture.
- Give every assertion a durable `because` rationale. Do not weaken a fixture merely to accommodate one adapter or make a flaky run green.
- Unit tests must not require an authenticated agent CLI. Mark live-agent coverage with `integration` and name the required adapter.
- Run the narrowest relevant `pytest` target while iterating, then the full suite. Run `scripts/release-check` from a clean neutral checkout before publication or release work.
- Update `README.md` and compatibility tests when changing CLI flags, fixture schemas, normalized actions, result envelopes, profiles, adapter behavior, or exit codes. Update `CHANGELOG.md` for every release.

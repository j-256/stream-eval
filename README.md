# stream-eval

Deterministic assertion harness for `claude -p` stream-json transcripts.

## What this does

Two scoring layers built around `claude -p` stream-json:

- **Trigger:** did the right skill fire? Pass = first `tool_use` in the transcript is the `Skill` tool with input matching the target skill name.
- **Synthesis:** did the right skill fire AND did the answer hold up to typed assertions (regex against final text, regex against tool inputs, tool-sequence-includes)?

A live dashboard surfaces in-flight runs of both kinds with a per-`(fixture, run)` segmented progress bar.

## Install

For most users (one-line global install via [pipx](https://pipx.pypa.io/stable/how-to/install-pipx/)):

```bash
pipx install git+<repo-url>
```

`pipx` puts `stream-eval` on `$PATH` permanently with its dependencies isolated; no venv to activate before each invocation. To update later, `pipx upgrade stream-eval`.

For development on stream-eval itself (editable install):

```bash
git clone <repo-url>
cd stream-eval
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
```

The `[dev]` extra adds `pytest` for the test suite. The `stream-eval` binary is on `$PATH` whenever the venv is active.

Either way, verify install with:

```bash
stream-eval trigger --help
stream-eval synthesis --help
stream-eval monitor --help
```

## Why this exists

The `skill-creator` plugin ships its own trigger-eval harness at `scripts/run_eval.py`. It works, but it has a footgun under common setups, and its scope is narrower than the work stream-eval is built for: stream-eval also runs synthesis-level evals, ships a live dashboard, detects worktree contamination, and handles upstream-API throttling cleanly – none of which `run_eval.py` addresses.

The footgun: `run_eval.py` writes a synthetic slash-command file at `<project>/.claude/commands/<skill-name>-skill-<uuid>.md` and scores by whether the model invokes that synthetic. When a real skill is also installed at `~/.claude/skills/<canonical-name>/` (which contributors often have, for dogfooding or interactive use), both entries appear in the catalog the model picks from – and empirically, under Sonnet 4.6, the model picks the canonical-named entry. The harness scores that as a miss. stream-eval's `--profile=isolated` (the default) builds a temp HOME with only the skill under test, so there's no canonical install to shadow the synthetic by construction.

See [`docs/comparison-with-skill-creator.md`](docs/comparison-with-skill-creator.md) for the full mechanism, the experiments we ran to validate the choice, and a side-by-side capability comparison.

## Architecture

```
        +-------------------+
        | subprocess.py     |   spawn + bail on api_retry budget
        |                   |   exhaustion or wall-clock timeout
        +---------+---------+
                  ^
                  |
        +---------+---------+
        | runner.py         |   ProcessPoolExecutor dispatch,
        |                   |   abort-on-first-timeout,
        |                   |   results envelope, canonical
        |                   |   stderr line, startup banner,
        |                   |   fixture-id assignment
        +----+---------+----+
             ^         ^
   +---------+--+   +--+----------+
   | trigger.py |   | synthesis.  |   thin wrappers: fixture
   |            |   | py          |   loading, scoring callback,
   +-----+------+   +------+------+   summary callback
         |                 |
         +--------+--------+
                  v
         +-----------------+
         | monitor.py      |   reads ps for live workers,
         |                 |   reads .output files for finished
         |                 |   runs, renders an HTML dashboard
         +-----------------+
```

Principle: the runner owns *how* runs are dispatched and aborted; the harnesses own *what* a run means.

## Files

| File | Responsibility |
|---|---|
| `stream_eval/env.py` | Tiny `.env` loader; runs at import time so harnesses can read `STREAM_EVAL_MODEL` etc. without external dependencies. |
| `stream_eval/subprocess.py` | `run_with_retry_aware_bail`: spawns `claude -p`, streams stdout to a transcript file, watches for `api_retry` events, bails when the CLI's retry budget is exhausted (`attempt == max_retries`) or a wall-clock backstop fires. Used by the runner to keep CLI internal retries from counting against the harness's wall clock. |
| `stream_eval/runner.py` | Shared library both harnesses delegate to. Owns: process-pool dispatch, abort-on-first-timeout, results-JSON envelope, canonical stderr progress line, startup banner, `assign_fixture_ids` with collision detection. Does NOT know fixture schemas or scoring rules. |
| `stream_eval/trigger.py` | Trigger-accuracy harness. Loads `trigger-eval.json`, validates, calls `run_eval` with `score_trigger_run` (which walks the transcript for the first `tool_use`) and a `summarize` callback (per-query trigger rate >= 0.5 matches `should_trigger`). |
| `stream_eval/synthesis.py` | Synthesis-behavior harness. Loads `synthesis-eval.json`, validates fixture schema and assertion kinds, calls `run_eval` with `score_synthesis_run` (parses transcript, evaluates typed assertions) and a strict-or-lenient `summarize` callback. |
| `stream_eval/monitor.py` | Live HTML dashboard. Walks `ps` (psutil) for trigger/synthesis workers and finished `.output` files, renders per-(skill, kind, harness-pid) state with a segmented bar. Per-row worker controls (`+1`/`-1`/pause/resume) talk to the running harness over a Unix socket and mutate `target_workers` / `dispatcher_state` live. |
| `tests/test_*.py` | Unit tests. Run with `pytest`. |

## Fixture schemas

### `trigger-eval.json`

A flat array of fixtures. Each fixture is one query the harness fires N times.

```json
[
  {
    "name": "scopes-shopper-products",
    "query": "what scopes does shopper-products getProducts need?",
    "should_trigger": true
  },
  {
    "query": "what's the difference between OCAPI and SCAPI?",
    "should_trigger": false
  }
]
```

| Field | Required | Description |
|---|---|---|
| `query` | yes | The prompt to send to `claude -p`. |
| `should_trigger` | yes | `true` if the skill should fire; `false` for a decline-test (the harness scores the inverse outcome as a pass). |
| `name` | optional | Stable id for the fixture. If omitted, the runner assigns `q0`, `q1`, ...; if the resulting `qN` collides with a hand-authored `name`, the runner skips that index and uses the next. |

Pass criterion (per fixture): trigger rate across runs >= 0.5 matches `should_trigger`. Default `--runs 3`, so 2-of-3 runs must trigger correctly.

### `synthesis-eval.json`

A flat array of fixtures. Each fixture has typed assertions evaluated against the resulting transcript.

```json
[
  {
    "name": "mcg-citation-leak",
    "query": "list the MCG references in the Salesforce dev catalog",
    "expected_skill": "dsc-scrape",
    "hypothesis": "MCG triggers dsc-scrape; final text cites only public URLs.",
    "assertions": [
      {
        "kind": "tool_input_matches",
        "tool": "Bash",
        "field": "command",
        "pattern": "dsc-scrape",
        "because": "MCG asks must route through dsc-scrape's catalog index"
      },
      {
        "kind": "final_text_excludes",
        "pattern": "/Users/.*\\.cache/dsc-scrape",
        "because": "answer must cite developer.salesforce.com URLs, not local cache paths"
      }
    ]
  }
]
```

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique id for the fixture (used for transcript filenames + stderr line + dashboard). Schema error if duplicated. |
| `query` | yes | The prompt. |
| `expected_skill` | optional | If set, the run fails when the first `tool_use` is `Skill` with a different skill name. |
| `hypothesis` | optional | Free-text note carried through to results.json for human review. |
| `assertions` | optional | Array of typed checks. Empty = `expected_skill` is the only criterion. |

Assertion kinds:

| Kind | Required fields | Behavior |
|---|---|---|
| `final_text_matches` | `pattern` | Regex against the final answer; fail if no match. |
| `final_text_excludes` | `pattern` | Regex against the final answer; fail if it matches. |
| `tool_input_matches` | `tool`, `field`, `pattern` | At least one tool_use of `tool` must have its `input.<field>` match `pattern`. |
| `tool_sequence_includes` | `pattern` | Regex against the newline-joined sequence of tool names; fail if no match. |

Every assertion takes a `because` field documenting the rule's intent; surfaced in failure reports verbatim.

Pass criterion (per fixture): `expected_skill` matched (if set) AND every assertion passed. Default mode is strict (every run of every fixture must pass); `--lenient` switches to majority-pass.

## Running an eval

`stream-eval trigger` and `stream-eval synthesis` share most of their CLI surface. Common flags first, then the per-harness defaults and extras.

### Common flags

These behave identically for both harnesses; only the defaults for `--runs` and `--timeout` differ (see the per-harness sections below).

| Flag | Description |
|---|---|
| `--eval` | Path to the fixture file (`trigger-eval.json` or `synthesis-eval.json`). Required. |
| `--out` | Where to write results.json. Required; parent dirs are created. |
| `--skill-path` | Path to the skill directory (containing `SKILL.md`). Required for the default `isolated` profile; the skill name is read from `SKILL.md`'s frontmatter `name:` field. |
| `--skill-name` | Override the skill name. Required when `--skill-path` is omitted (for `restricted` or `inherit` profiles). |
| `--also-install` | Path to a sibling skill to install alongside the skill under test. May be repeated. Only effective under the `isolated` profile. |
| `--profile` | `isolated` (default), `restricted`, or `inherit`. See [Profiles](#profiles). |
| `--workers` | Concurrent `claude -p` subprocesses. Default: `4`. |
| `--cwd` | CWD for `claude -p` subprocesses. Default: current dir. |

**Path-based input.** The harness reads the skill name from `<skill-path>/SKILL.md`'s frontmatter `name:` field. This decouples the skill's directory layout from its canonical name and avoids silent breakage when a directory is renamed.

Both harnesses retain per-run stream-json transcripts at `runs/<iteration>/transcripts/<out-stem>/<fixture>-<N>.jsonl` for offline debugging.

Exit codes are also shared:

| Code | Meaning |
|---|---|
| 0 | All fixtures pass. |
| 1 | At least one fixture fails. |
| 2 | Fixture schema error, returned before any runs spawn. |
| 3 | Aborted on api_retry budget exhaustion or wall-clock timeout. Continuing measurements after a budget-exhaustion event would mix real failures with throttle noise; re-run when the upstream API has recovered. |

### Trigger

```bash
stream-eval trigger \
    --eval evals/dsc-endpoint-help/trigger-eval.json \
    --skill-path skills/dsc-endpoint-help \
    --runs 3 --workers 4 --timeout 1800 \
    --out evals/dsc-endpoint-help/runs/iteration-N/results.json
```

| Flag | Default | Description |
|---|---|---|
| `--runs` | `3` | Runs per fixture. |
| `--timeout` | `1800` | Per-run wall-clock backstop, in seconds. Counts retry-backoff time, which compounds fast under throttle (we've seen starting waits around 60s). Trigger runs themselves are short (~10-40s without retries), so this default holds up under light/moderate throttle; under heavy throttle, see [Limitations](#limitations-and-out-of-scope) and consider bumping. |

### Synthesis

```bash
stream-eval synthesis \
    --eval evals/dsc-scrape/synthesis-eval.json \
    --skill-path skills/dsc-scrape \
    --runs 5 --workers 4 --timeout 600 \
    --out evals/dsc-scrape/runs/iteration-N/results.json
```

| Flag | Default | Description |
|---|---|---|
| `--runs` | `5` | Higher than trigger because assertion failures can be noisy. |
| `--timeout` | `600` | Per-run wall-clock backstop. Counts retry-backoff time, same as trigger. Synthesis runs without retries are typically a few minutes; the 10-minute default leaves room for a couple of retries before retry-budget-exhaustion has a chance to fire. Under heavier throttle, bump it (or run fewer workers); see [Limitations](#limitations-and-out-of-scope). |
| `--lenient` | off | Pass if majority of runs pass. Default is strict (every run must pass every assertion). |

### Profiles

Three semantic profiles control how the spawn sees the user's environment:

| Profile | Skills visible | MCP / Agent | When to use |
|---|---|---|---|
| `isolated` (default) | only `--skill-path` (+ `--also-install` if any) | none | Production-equivalent for a vanilla install. The skill is tested as a user with nothing else would experience it. |
| `restricted` | user's globally-installed skills | none (stripped) | Tests a skill against the user's other globally-installed skills but without MCP/Agent. |
| `inherit` | user's globally-installed skills | user's MCP + Agent | Closest to interactive use. Useful for diagnostic runs where you want to see what the agent reaches for given everything. |

Set via the `STREAM_EVAL_PROFILE` env var or `--profile <name>`.

## The dashboard

```bash
# one-shot CLI summary
stream-eval monitor

# HTML dashboard at http://localhost:8765
stream-eval monitor serve

# pin to a specific Claude Code session by name or UUID prefix
stream-eval monitor serve --session my-session-name
stream-eval monitor serve --session 0fc37026

# auto-open in default browser
stream-eval monitor serve --open
```

What it shows:

- **Per-eval rows, keyed by `(skill, kind, harness pid)`.** Two concurrent evals of the same skill+kind get distinct rows; the harness pid in the header lets you tell them apart.
- **Status state machine.** Each row is `active` / `completed` / `aborted` / `unknown`. Sort order: active first, then youngest-first within each status bucket.
- **Segmented progress bar.** One cell per `(fixture, run)`. Green = pass, red = fail, pulsing gray = in-flight, light gray = pending. Yellow outline = worktree-contaminated (pass verdict unaudited).
- **Inline Active Workers table** (collapsible, expanded by default). Per active row: claude pid, fixture, run, started, retries, attempt N/M, last error.
- **Inline Recent Completions table** (collapsible). Last 5 cells per row.
- **Per-row worker controls.** `+1`/`-1`/pause/resume buttons on each active row talk to that harness's Unix socket. Disabled when no live socket. A `running`/`paused` badge surfaces dispatcher state.
- **Session scoping.** The dashboard pins to one Claude Code session at a time. Layered detection: explicit `--session` flag, then this dashboard's own bash parent's `.output` file, then any live trigger/synthesis worker's bash parent, then the youngest few `.output` files globally.
- **Configurable polling.** Default 5s when active runs exist, 30s when idle (override via inputs in the dashboard header or `STREAM_EVAL_POLL_*_MS` env). Auto-pauses after ~3 min of no change; click "refresh now" or change an interval to resume.
- **Persistent UI state.** Each `<details>` block's open/closed state persists across re-renders via localStorage.

Stop with Ctrl-C. The monitor process is decoupled from in-flight evals; restarting it doesn't affect them.

## Stderr line and startup banner

Both harnesses go through the runner, which emits one canonical line shape per completed run and one banner at startup.

Progress line:
```
[N/M] kind=<trigger|synthesis> pass=<True|False> fixture_id=<id> run=<R> elapsed=<s>s retries=<n> timeout_reason=<none|retry_budget|wall_clock> first_tool=<tool|-> first_skill=<skill|-> failed_asserts=<n>: <query truncated to 80>
```

All trailing fields (`timeout_reason`, `first_tool`, `first_skill`, `failed_asserts`) are required on every line. Sentinel values for kind-irrelevant slots: `timeout_reason=none`, `first_tool=-` / `first_skill=-` (no tool fired), `failed_asserts=0` (trigger or synthesis-with-all-assertions-passing).

Startup banner (emitted once before the first task completes):
```
=== eval starting: kind=<kind> skill=<skill> eval=<eval-path> runs=<R> workers=<W> total_fixtures=<N> pid=<harness-pid> ===
```

Finish banner (emitted after the last task scores or the harness aborts):
```
=== eval finished: kind=<kind> skill=<skill> pid=<harness-pid> verdict=<completed|aborted> ===
```

The dashboard joins startup and finish by `pid` to distinguish active runs from completed and aborted ones, and routes per-row worker-control buttons to `/tmp/stream-eval-<pid>.sock`. `tail -f` on the underlying `.output` file is human-readable as-is.

## Dashboard development: fake scenarios

`stream_eval.fake` synthesizes `.output` files and stateful in-memory socket servers for every dashboard state without running real evals. Useful for visual smoke testing, regression harnesses for dashboard changes, and reproducing routing-class bugs.

Run interactively:
```
stream-eval fake list                          # show all scenarios
stream-eval fake concurrent                    # two active evals (pid routing)
stream-eval fake concurrent,over-cap,legacy    # several at once
stream-eval fake all                           # every scenario, simultaneously
```

Real operation has every state coexisting; pass several scenarios (comma-separated) or `all` to render them simultaneously. The single `full-spread` scenario is a curated subset (one of each state); `all` renders every scenario including the larger ones.

The driver synthesizes the scenarios directly into `~/.claude/projects/stream-eval-fake-<id>/` so the dashboard's file walk picks them up, and blocks on Ctrl+C. Tear-down removes the directory and unlinks the fake sockets.

Programmatic use (tests, dev scripts):
```python
from stream_eval.fake import make_fake_state
from stream_eval.monitor.state import build_state

# One scenario:
with make_fake_state("concurrent") as state:
    rows = build_state(state.output_paths, is_pid_alive=state.is_pid_alive).rows
    # ... assertions on rows

# Several composed:
with make_fake_state(["concurrent", "over-cap", "legacy"]) as state:
    rows = build_state(state.output_paths, is_pid_alive=state.is_pid_alive).rows
```

Scenarios cover: `active-clean`, `active-with-failures`, `active-with-contamination`, `concurrent`, `completed`, `aborted`, `aborted-no-finish-banner`, `legacy`, `over-cap`, `full-spread`. See `stream_eval/fake/runs.py` for builders – adding a scenario is one function plus an entry in `SCENARIOS`.

## Configuration via `.env`

The harnesses read configuration from `.env` at the repo root (gitignored) via `stream_eval/env.py`. See `.env.example` for the full list. Common knobs:

| Variable | Default | Description |
|---|---|---|
| `STREAM_EVAL_MODEL` | `sonnet` | Model identifier passed to `claude -p --model`. Pin an exact identifier (e.g. `claude-sonnet-4-6`) rather than the `sonnet` alias if your CLI's alias resolution doesn't target the version you want – some deployments resolve `sonnet` to an older release. |
| `STREAM_EVAL_PROFILE` | `isolated` | Toolbelt profile (`isolated`, `restricted`, or `inherit`) for spawned `claude -p` subprocesses. |
| `STREAM_EVAL_OUTPUT_LIMIT` | `100` | Maximum `.output` files the dashboard parses on each refresh, ranked by mtime descending. Bump it if a slow active eval whose mtime falls outside the top-N starts disappearing mid-run. |
| `STREAM_EVAL_PER_SKILL_CAP` | `5` | Per-(skill, kind) row cap. Active rows always bypass the cap; older completed/aborted rows are hidden once the cap is reached. Set to `0` to disable. |
| `STREAM_EVAL_POLL_ACTIVE_MS` | `5000` | Default dashboard poll interval when active runs exist. Operators can override per-tab via the input at the top-right; the override persists in localStorage. |
| `STREAM_EVAL_POLL_IDLE_MS` | `30000` | Default dashboard poll interval when no active runs. Same per-tab override mechanism. |

Existing environment values win over `.env`; `.env` only fills gaps. No-op if `.env` is missing.

## Results envelope: `harness_version`

Every results.json the harness writes includes:

```json
{
  "harness_version": "abc1234...",
  "harness_version_kind": "git_sha",
  ...
}
```

`harness_version_kind` is one of:

- `"git_sha"`: read from the harness's `.git/HEAD`. Stable across runs at the same checkout; changes when you update the submodule or switch branches.
- `"package_version"`: read from `stream_eval.__version__`. Reported when the harness is pip-installed from a release tarball with no `.git` available.
- `"unknown"`: neither lookup succeeded. Should not happen in normal installs.

Iteration notes can cite this directly so eval numbers stay correlatable across harness churn.

## Limitations and out-of-scope

- **Single-host only.** No multi-machine eval distribution.
- **The dashboard is single-session-pinned.** Two parallel Claude Code sessions each running their own evals appear in two separate dashboards; cross-session aggregation is not supported.
- **Throughput is upstream-limited.** Running with `--workers 4` against an unloaded API endpoint is roughly 4x of `--workers 1`; against a loaded one, throttle dominates and `--workers 2` may match `--workers 4` at lower retry frequency. The dashboard surfaces retry events; treat high retry rates as a signal to lower workers.
- **Wall-clock `--timeout` counts retry-backoff time, and that interacts badly with the api_retry-exhaustion bail.** When the upstream throttles, the CLI retries with backoff – we've observed starting waits around 60s, but the full schedule (whether it caps, plateaus, or grows further) is internal to the CLI and we haven't characterized it. What we do know: a sustained throttle storm can blow trigger's 1800s default before retry 10 is reached; synthesis's default was previously 240s and could blow up after a single retry, which is why we bumped it to 600s as a stopgap until the retry-aware fix lands. Two consequences. **First, ambiguous failure mode:** a run that wall-clocks out under load could be a stuck skill (model in a tool-use loop) or just heavy throttle, and the harness reports the same `wall_timed_out=True` for both. **Second, signal stomping:** the api_retry-exhaustion bail (`attempt == max_retries`) is supposed to be the principled signal for "give up, upstream is poisoned" – but the wall clock fires before retry 10 is reached under realistic throttle, pre-empting that cleaner signal and reporting wall-clock-failure where exhaustion would have been the right verdict. The honest fix is to make the wall clock retry-aware: subtract retry-backoff time from the deadline so it reflects model-thinking time only, leaving api_retry-exhaustion as the clean throttle-bail signal. Until that lands, the operator workaround under heavy throttle is to bump `--timeout` generously enough that retry-budget-exhaustion can fire first. If iteration baselines start showing systematic wall-clock failures correlated with API load, this is the place to look.

## License

MIT. See [LICENSE](LICENSE).

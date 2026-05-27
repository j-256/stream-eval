# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Flask + Jinja2 + psutil dashboard, decomposed into a monitor/ subpackage:
  monitor/app.py (Flask routes), monitor/state.py (DashboardState),
  monitor/ps.py (psutil-based discovery + session detection),
  monitor/parsing.py (stderr-line and banner regexes), monitor/socket_client.py
  (talks to harness over Unix socket), monitor/templates/ (Jinja2),
  monitor/static/ (CSS).
- Worker-control routes: POST /workers/+1, /workers/-1, /workers/pause,
  /workers/resume.
- `harness_version` and `harness_version_kind` fields in every results.json envelope, derived from .git/HEAD or stream_eval.__version__.
- Initial repository scaffolding.
- Imported eval harness from claude-code-skills/tools/.
- Single `stream-eval` CLI binary with `trigger`, `synthesis`, `monitor` subcommands.
- Hermetic skill isolation via per-spawn temp HOME (`stream_eval.isolation`).
- `--skill-path` and `--also-install` flags on `trigger` and `synthesis` subcommands.
- New `isolated` profile (now the default), running each spawn against a temp HOME containing only the skill under test.
- `atexit` reaper for orphaned `stream-eval-<pid>-*` temp dirs.
- Dynamic worker pool (`stream_eval.pool.Dispatcher`) replacing
  `concurrent.futures.ProcessPoolExecutor`. Supports live
  `target_workers` adjustment with drain-on-decrement semantics.
- `Dispatcher.stop()` for clean abort path (used by abort-on-first-timeout).
- SIGUSR1 / SIGUSR2 signal handlers to decrement / increment
  `target_workers` from any terminal (`kill -USR1 <pid>`).
- Unix-socket control listener at `/tmp/stream-eval-<pid>.sock`
  (line protocol: GET/SET workers, PAUSE/RESUME, GET state, QUIT).

### Removed
- Single-file monitor.py (replaced by monitor/ subpackage).
- Legacy test_monitor.py (replaced by per-submodule tests).

### Changed
- Renamed `DSC_EVAL_MODEL` -> `STREAM_EVAL_MODEL` and `DSC_EVAL_PROFILE` -> `STREAM_EVAL_PROFILE`.
- Hyphenated CLI scripts (`trigger-eval.py` etc.) are now Python modules under `stream_eval/`.
- `--skill-name` is now optional; defaults to the `name:` field in `<skill-path>/SKILL.md`.
- The previous `default` profile is renamed to `inherit`. Update any `--profile default` invocations to `--profile inherit`.

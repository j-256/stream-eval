# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial repository scaffolding.
- Imported eval harness from claude-code-skills/tools/.
- Single `stream-eval` CLI binary with `trigger`, `synthesis`, `monitor` subcommands.
- Hermetic skill isolation via per-spawn temp HOME (`stream_eval.isolation`).
- `--skill-path` and `--also-install` flags on `trigger` and `synthesis` subcommands.
- New `isolated` profile (now the default), running each spawn against a temp HOME containing only the skill under test.
- `atexit` reaper for orphaned `stream-eval-<pid>-*` temp dirs.

### Changed
- Renamed `DSC_EVAL_MODEL` -> `STREAM_EVAL_MODEL` and `DSC_EVAL_PROFILE` -> `STREAM_EVAL_PROFILE`.
- Hyphenated CLI scripts (`trigger-eval.py` etc.) are now Python modules under `stream_eval/`.
- `--skill-name` is now optional; defaults to the `name:` field in `<skill-path>/SKILL.md`.
- The previous `default` profile is renamed to `inherit`. Update any `--profile default` invocations to `--profile inherit`.

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial repository scaffolding.
- Imported eval harness from claude-code-skills/tools/.
- Single `stream-eval` CLI binary with `trigger`, `synthesis`, `monitor` subcommands.

### Changed
- Renamed `DSC_EVAL_MODEL` -> `STREAM_EVAL_MODEL` and `DSC_EVAL_PROFILE` -> `STREAM_EVAL_PROFILE`.
- Hyphenated CLI scripts (`trigger-eval.py` etc.) are now Python modules under `stream_eval/`.

# stream-eval

Deterministic assertion harness for `claude -p` stream-json transcripts.

## What this does

Two scoring layers built around `claude -p` stream-json:

- **Trigger:** did the right skill fire? Pass = first `tool_use` in the transcript is the `Skill` tool with input matching the target skill name.
- **Synthesis:** did the right skill fire AND did the answer hold up to typed assertions (regex against final text, regex against tool inputs, tool-sequence-includes)?

A live dashboard surfaces in-flight runs of both kinds with a per-`(fixture, run)` segmented progress bar.

## Install

```bash
git clone <repo-url>
cd stream-eval
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
```

After install, `stream-eval` is on `$PATH`:

```bash
stream-eval trigger --help
stream-eval synthesis --help
stream-eval monitor --help
```

## Quickstart

(Filled in by Phase C once the new CLI shape lands.)

## Architecture

(Filled in by Phase B once `tools/README.md` content is migrated.)

## License

MIT. See [LICENSE](LICENSE).

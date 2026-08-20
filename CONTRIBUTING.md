# Contributing to stream-eval

## Choose the source branch

The hosted `main` branch is a generated distribution tip and can be replaced after any publication. The stable, host-neutral development history lives on `source`. A force update of `main` does not affect a topic branch based on `source`.

After cloning, create a topic branch from `source` rather than `main`:

```bash
git fetch origin source
git switch -c my-change --no-track origin/source
```

If the canonical repository uses a remote other than `origin`, substitute that remote name. Push the topic branch to a writable remote and set the pull request's base branch to `source`. Keep repository references host-neutral in source content; the publisher supplies concrete clone URLs.

To catch up while a pull request is open, rebase onto the latest source branch:

```bash
git fetch origin source
git rebase origin/source
```

If work was accidentally based on generated `main`, preserve a backup branch and replay only the contribution commits onto `source`:

```bash
git branch backup/my-change
git fetch origin source
git rebase --onto origin/source <generated-main-tip> my-change
```

`<generated-main-tip>` is the generated commit that the topic branch originally started from. If that boundary is unclear or generated `main` was merged into the topic, stop and ask a maintainer to inspect the graph rather than guessing.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
```

## Running tests

```bash
pytest                       # full suite
pytest tests/test_<module>.py  # one file
pytest -k "isolation"        # by name pattern
pytest -v                    # verbose
```

Tests that need a real `claude -p` invocation are marked with `@pytest.mark.integration` and skipped by default in CI.

## Style

- Python 3.11+. Type hints encouraged but not enforced.
- ASCII-only in source code, comments, and docstrings.
- Test files mirror module names: `tests/test_<module>.py`.
- One concern per test file when practical.

## Commit message style

Conventional Commits:
- `feat(<area>):` new functionality
- `fix(<area>):` bug fix
- `refactor(<area>):` internal cleanup, no user-visible change
- `docs(...):` README / docstring changes
- `test(...):` test changes
- `chore(...):` scaffolding, gitignore, cross-cutting cleanups

## Versioning

SemVer. Pre-1.0 (`0.x.y`) for the entire pre-stable phase.
Breaking changes increment minor; bug fixes increment patch.
`CHANGELOG.md` updated for every release.

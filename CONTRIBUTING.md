# Contributing to stream-eval

## Development setup

```bash
git clone <repo-url>
cd stream-eval
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

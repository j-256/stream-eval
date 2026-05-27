"""Build DashboardState from .output files.

The runner emits a startup banner plus one progress line per completed
run. This module re-reads those files and aggregates per-(skill, kind)
into a structure the templates render.

Public surface:
- DashboardState: one snapshot of all skill rows.
- DashboardRow: per-(skill, kind) row.
- DashboardCell: per-(fixture_id, run) cell in the segmented bar.
- build_state(output_paths): construct DashboardState from .output paths.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from stream_eval.monitor.parsing import (
    parse_progress_line,
    parse_startup_banner,
)


@dataclass
class DashboardCell:
    fixture_id: str
    run: int
    pass_: Optional[bool]  # None = pending
    contaminated: bool = False


@dataclass
class DashboardRow:
    skill: str
    kind: str
    total_fixtures: int
    runs: int
    cells: list = field(default_factory=list)
    eval_path: Optional[str] = None


@dataclass
class DashboardState:
    rows: list = field(default_factory=list)


def build_state(output_paths):
    """Parse each .output file and build per-(skill, kind) rows.

    A row is keyed by (skill, kind). The startup banner sets total_fixtures
    and runs (denominator); each progress line fills one cell."""
    rows_by_key = {}
    for path in output_paths:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        current_key = None
        for line in text.splitlines():
            banner = parse_startup_banner(line)
            if banner:
                key = (banner["skill"], banner["kind"])
                row = rows_by_key.setdefault(
                    key,
                    DashboardRow(
                        skill=banner["skill"],
                        kind=banner["kind"],
                        total_fixtures=banner["total_fixtures"],
                        runs=banner["runs"],
                        eval_path=banner["eval"],
                    ),
                )
                row.total_fixtures = banner["total_fixtures"]
                row.runs = banner["runs"]
                current_key = key
                continue

            prog = parse_progress_line(line)
            if not prog:
                continue
            if current_key is None:
                continue
            row = rows_by_key.setdefault(
                current_key,
                DashboardRow(
                    skill=current_key[0], kind=current_key[1],
                    total_fixtures=0, runs=0,
                ),
            )
            row.cells.append(DashboardCell(
                fixture_id=prog["fixture_id"],
                run=prog["run"],
                pass_=prog["pass_"],
                contaminated=prog["contaminated"],
            ))

    rows = list(rows_by_key.values())
    rows.sort(key=lambda r: (r.skill, r.kind))
    return DashboardState(rows=rows)

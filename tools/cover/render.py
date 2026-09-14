"""Render the product dashboard with synthetic state and no process discovery."""
import sys
from pathlib import Path

from flask import render_template

from stream_eval.monitor.app import create_app
from stream_eval.monitor.state import DashboardCell, DashboardRow, DashboardState

NOW = 1768487400
rows = []
examples = [
    ('dsc-endpoint-help', 'codex', 'synthesis', 'active', 28),
    ('dsc-scenario', 'claude', 'trigger', 'active', 20),
    ('dsc-scrape', 'opencode', 'synthesis', 'completed', 40),
]
for index, (skill, agent, kind, status, done) in enumerate(examples):
    row = DashboardRow(skill=skill, agent=agent, kind=kind, total_fixtures=20, runs=2,
        harness_pid=4100 + index, status=status, started_at=NOW - 720,
        finished_at=NOW - 60 if status == 'completed' else None,
        cells=[DashboardCell(fixture_id=f'example-{number + 1}', run=number % 2, pass_=number % 13 != 12) for number in range(done)])
    row.target_workers = 3
    row.dispatcher_state = 'running'
    row.in_flight_count = 0 if status == 'completed' else 3
    row.in_flight_retries = 0
    row.total_retries = 0
    row.recent = []
    row.workers_for_row = [] if status == 'completed' else [dict(
        agent=agent, pid=4200 + index * 10 + worker, fixture_id=f'example-{done + worker + 1}',
        run=1, started_at_human='14:28:00', retries=0, latest_attempt=None,
        max_retries_field=None, last_error=None) for worker in range(3)]
    rows.append(row)

app = create_app(session='example-evaluation')
with app.test_request_context('/'):
    html = render_template('dashboard.html', state=DashboardState(rows), session='example-evaluation',
        poll_active_ms=5000, poll_idle_ms=30000, now=NOW)
Path(sys.argv[1]).write_text(html)

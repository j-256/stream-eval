"""Filesystem locations owned by stream-eval"""
import os
from pathlib import Path


def state_dir():
    configured = os.environ.get("STREAM_EVAL_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser() / "stream-eval"
    return Path.home() / ".local" / "state" / "stream-eval"


def legacy_state_dir():
    return Path.home() / ".claude" / "projects"


def output_dirs():
    primary = state_dir()
    legacy = legacy_state_dir()
    return (primary,) if primary == legacy else (primary, legacy)

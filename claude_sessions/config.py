"""Persisted settings for claude-sessions-tui.

Resolution order for the sessions folder (highest priority first):
  1. CLI flag  --projects-dir PATH
  2. env var   CLAUDE_SESSIONS_DIR
  3. config file (set via the in-app settings screen)
  4. default   ~/.claude/projects
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from platformdirs import user_config_path

DEFAULT_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
ENV_VAR = "CLAUDE_SESSIONS_DIR"
CONFIG_FILE = user_config_path("claude-sessions-tui") / "config.json"


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value)).expanduser()


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def set_projects_dir(path: str | Path) -> None:
    cfg = load_config()
    cfg["projects_dir"] = str(path)
    save_config(cfg)


def resolve_projects_dir(cli_value: str | None = None) -> Path:
    """Return the sessions folder honoring CLI > env > config > default."""
    if cli_value:
        return _expand(cli_value)
    env = os.environ.get(ENV_VAR)
    if env:
        return _expand(env)
    configured = load_config().get("projects_dir")
    if configured:
        return _expand(configured)
    return DEFAULT_PROJECTS_DIR

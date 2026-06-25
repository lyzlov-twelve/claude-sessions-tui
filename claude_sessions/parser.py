"""Parser for Claude Code session jsonl files.

Sessions live in ~/.claude/projects/<encoded-project-path>/<sessionId>.jsonl.
Each line is a standalone JSON event with its own `type`:
  - custom-title / ai-title  — session title (custom takes priority over AI)
  - last-prompt              — the user's last prompt
  - user / assistant         — messages (we look for tool_use inside assistant)
  - pr-link                  — linked MR/PR
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from glob import glob
from pathlib import Path

PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))


@dataclass
class Session:
    session_id: str
    project: str           # human-readable project name
    project_dir: str       # folder name in ~/.claude/projects
    file_path: str

    cwd: str | None = None

    custom_title: str | None = None
    ai_title: str | None = None
    first_prompt: str | None = None
    last_prompt: str | None = None

    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0

    started_at: datetime | None = None
    ended_at: datetime | None = None

    pr_url: str | None = None
    pr_number: int | None = None
    pr_repo: str | None = None

    agent_names: set[str] = field(default_factory=set)

    @property
    def title(self) -> str:
        for candidate in (self.custom_title, self.ai_title, self.first_prompt):
            if candidate:
                return candidate.strip().splitlines()[0][:120]
        return "(untitled)"

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def duration(self) -> str:
        if not self.started_at or not self.ended_at:
            return "—"
        secs = max(0, int((self.ended_at - self.started_at).total_seconds()))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    @property
    def last_activity(self) -> str:
        if not self.ended_at:
            return "—"
        now = datetime.now(timezone.utc)
        secs = max(0, int((now - self.ended_at).total_seconds()))
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"

    @property
    def sort_key(self) -> float:
        return self.ended_at.timestamp() if self.ended_at else 0.0


def _decode_project_name(dir_name: str) -> str:
    """`-Users-ylyzlov-Projects-access-frontend` -> `access-frontend`."""
    marker = "-Projects-"
    if marker in dir_name:
        return dir_name.split(marker, 1)[1]
    return dir_name.lstrip("-")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _extract_text(content) -> str | None:
    """Extract text from message.content (a string or a list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None  # not a real user prompt
            if block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
        return "\n".join(parts) if parts else None
    return None


def parse_session(file_path: str, project: str, project_dir: str) -> Session:
    session_id = Path(file_path).stem
    s = Session(session_id=session_id, project=project,
                project_dir=project_dir, file_path=file_path)

    with open(file_path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = d.get("type")

            if s.cwd is None and d.get("cwd"):
                s.cwd = d["cwd"]

            if t == "custom-title":
                s.custom_title = d.get("customTitle")
            elif t == "ai-title":
                s.ai_title = d.get("aiTitle")
            elif t == "last-prompt":
                s.last_prompt = d.get("lastPrompt")
            elif t == "agent-name":
                if d.get("agentName"):
                    s.agent_names.add(d["agentName"])
            elif t == "pr-link":
                s.pr_url = d.get("prUrl")
                s.pr_number = d.get("prNumber")
                s.pr_repo = d.get("prRepository")
            elif t == "user":
                s.user_messages += 1
                msg = d.get("message") or {}
                text = _extract_text(msg.get("content"))
                if text and s.first_prompt is None:
                    s.first_prompt = text
            elif t == "assistant":
                s.assistant_messages += 1
                msg = d.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    s.tool_calls += sum(
                        1 for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                    )

            ts = _parse_ts(d.get("timestamp"))
            if ts:
                if s.started_at is None or ts < s.started_at:
                    s.started_at = ts
                if s.ended_at is None or ts > s.ended_at:
                    s.ended_at = ts

    return s


def scan_sessions(projects_dir: Path = PROJECTS_DIR) -> list[Session]:
    """Scan all projects and return sessions, newest first."""
    sessions: list[Session] = []
    for jsonl in glob(str(projects_dir / "*" / "*.jsonl")):
        path = Path(jsonl)
        dir_name = path.parent.name
        project = _decode_project_name(dir_name)
        try:
            sessions.append(parse_session(str(path), project, dir_name))
        except OSError:
            continue
    sessions.sort(key=lambda s: s.sort_key, reverse=True)
    return sessions

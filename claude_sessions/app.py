"""Textual TUI for browsing Claude Code sessions."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from .parser import Session, scan_sessions

ALL = "__all__"

# Launch flags: (label, extra args appended to `claude --resume <id>`).
# Multiple options can be selected at once; their args are concatenated.
LAUNCH_OPTIONS: list[tuple[str, list[str]]] = [
    ("Fork into a new session  (--fork-session)", ["--fork-session"]),
    ("Model: Opus  (--model opus)", ["--model", "opus"]),
    ("Model: Sonnet  (--model sonnet)", ["--model", "sonnet"]),
    ("Plan mode  (--permission-mode plan)", ["--permission-mode", "plan"]),
    ("Auto-accept edits  (--permission-mode acceptEdits)",
     ["--permission-mode", "acceptEdits"]),
    ("Effort: high  (--effort high)", ["--effort", "high"]),
    ("⚠ Skip permission checks  (--dangerously-skip-permissions)",
     ["--dangerously-skip-permissions"]),
]


class LaunchDialog(ModalScreen[list[str] | None]):
    """Confirm launching a session and pick claude flags (multi-select).

    Returns the list of extra args on confirm, or None on cancel.
    """

    CSS = """
    LaunchDialog { align: center middle; }
    #dialog {
        width: 76;
        height: auto;
        max-height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #dlg-title { text-style: bold; color: $accent; }
    #dlg-sub { color: $text-muted; }
    #dlg-q { padding: 1 0 0 0; text-style: bold; }
    #dlg-opts-label { padding: 1 0 0 0; color: $accent; }
    #opts { width: 100%; height: auto; }
    #dlg-buttons { height: auto; align: center middle; padding-top: 1; }
    #dlg-buttons Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        s = self.session
        with Vertical(id="dialog"):
            yield Label(f"Session: {s.title}", id="dlg-title")
            yield Label(f"{s.short_id}  ·  {s.project}  ·  {s.cwd or '—'}", id="dlg-sub")
            yield Label("Open this session in this window?", id="dlg-q")
            yield Label("Launch options (select any, Space to toggle):",
                        id="dlg-opts-label")
            yield SelectionList[int](
                *(Selection(label, i) for i, (label, _) in enumerate(LAUNCH_OPTIONS)),
                id="opts",
            )
            with Horizontal(id="dlg-buttons"):
                yield Button("Yes, open", variant="success", id="yes")
                yield Button("No", variant="error", id="no")

    def _selected_args(self) -> list[str]:
        chosen = sorted(self.query_one("#opts", SelectionList).selected)
        return [arg for i in chosen for arg in LAUNCH_OPTIONS[i][1]]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes":
            self.dismiss(self._selected_args())
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionsApp(App):
    CSS = """
    Screen { layout: horizontal; }

    #sidebar {
        width: 30;
        border-right: solid $primary;
    }
    #sidebar > Label {
        padding: 0 1;
        text-style: bold;
        color: $accent;
    }
    #projects { height: 1fr; }

    #right { width: 1fr; }

    #search { display: none; dock: top; }
    #search.visible { display: block; }

    #table { height: 1fr; }

    #detail {
        height: 45%;
        border-top: solid $primary;
        padding: 0 1;
    }
    #detail-body { padding: 1 1; }
    """

    BINDINGS = [
        Binding("right", "focus_sessions", "To sessions →", show=False, priority=True),
        Binding("left", "focus_projects", "← To projects", show=False, priority=True),
        Binding("enter", "open_session", "Open in Claude"),
        Binding("c", "open_session", "Open in Claude", show=False),
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "search", "Search"),
        Binding("escape", "clear_search", "Clear search", show=False),
        Binding("tab", "focus_next", "Focus", show=False),
    ]

    TITLE = "Claude Code — Sessions"

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[Session] = []
        self.visible_sessions: list[Session] = []
        self.project_filter: str = ALL
        self.query: str = ""
        self.current: Session | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sidebar"):
            yield Label("PROJECTS")
            yield ListView(id="projects")
        with Vertical(id="right"):
            yield Input(placeholder="Search by title / prompt / id…", id="search")
            yield DataTable(id="table")
            with VerticalScroll(id="detail"):
                yield Static(id="detail-body")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        table.add_column("Title", width=17)
        table.add_columns("ID", "Msgs", "Tools", "Duration", "Activity")
        await self.load()

    # ── data ────────────────────────────────────────────────────────────

    async def load(self) -> None:
        self.sessions = scan_sessions()
        await self.rebuild_projects()
        self.rebuild_table()

    async def rebuild_projects(self) -> None:
        counts = Counter(s.project for s in self.sessions)
        lv = self.query_one("#projects", ListView)
        # clear() is async — wait for old items to be removed, otherwise the
        # new ones get inserted with the same IDs (DuplicateIds).
        await lv.clear()
        lv.append(ListItem(Label(f"All  ({len(self.sessions)})"), id="p___all__"))
        for i, project in enumerate(sorted(counts)):
            label = f"{project}  ({counts[project]})"
            # widget id is the index (unique); the project name lives in `name`
            lv.append(ListItem(Label(label), id=f"p_{i}", name=project))

    def filtered(self) -> list[Session]:
        result = self.sessions
        if self.project_filter != ALL:
            result = [s for s in result if s.project == self.project_filter]
        if self.query:
            q = self.query.lower()
            result = [
                s for s in result
                if q in s.title.lower()
                or q in s.session_id.lower()
                or (s.first_prompt and q in s.first_prompt.lower())
                or (s.last_prompt and q in s.last_prompt.lower())
            ]
        return result

    def rebuild_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        self.visible_sessions = self.filtered()
        for s in self.visible_sessions:
            title = s.title if len(s.title) <= 17 else s.title[:16] + "…"
            table.add_row(
                title,
                s.short_id,
                str(s.user_messages + s.assistant_messages),
                str(s.tool_calls),
                s.duration,
                s.last_activity,
                key=s.session_id,
            )
        if self.visible_sessions:
            table.move_cursor(row=0)
            self.show_detail(self.visible_sessions[0])
        else:
            self.current = None
            self.query_one("#detail-body", Static).update("[dim]Nothing found[/dim]")

    # ── detail panel ──────────────────────────────────────────────────────

    def show_detail(self, s: Session) -> None:
        self.current = s
        lines: list[str] = []
        lines.append(f"[b cyan]{escape(s.title)}[/b cyan]")
        lines.append("")
        lines.append(f"[b]ID:[/b]       {s.session_id}")
        lines.append(f"[b]Project:[/b]  {escape(s.project)}")
        if s.custom_title:
            lines.append(f"[b]Custom title:[/b] {escape(s.custom_title)}")
        if s.ai_title and s.ai_title != s.title:
            lines.append(f"[b]AI title:[/b] {escape(s.ai_title)}")

        stats = (
            f"💬 {s.user_messages + s.assistant_messages} msgs  "
            f"(user {s.user_messages} / asst {s.assistant_messages})   "
            f"🔧 {s.tool_calls} tool calls   ⏱ {s.duration}   "
            f"🕓 {s.last_activity}"
        )
        lines.append("")
        lines.append(stats)
        if s.started_at:
            started = s.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
            lines.append(f"[dim]Started: {started}[/dim]")
        if s.agent_names:
            lines.append(f"[b]Agents:[/b] {escape(', '.join(sorted(s.agent_names)))}")
        if s.pr_url:
            pr = f"#{s.pr_number} {s.pr_repo}" if s.pr_number else s.pr_url
            lines.append(f"[b]PR/MR:[/b] {escape(pr)}")

        if s.first_prompt:
            lines.append("")
            lines.append("[b yellow]First prompt:[/b yellow]")
            lines.append(escape(s.first_prompt.strip()[:600]))
        if s.last_prompt and s.last_prompt.strip() != (s.first_prompt or "").strip():
            lines.append("")
            lines.append("[b yellow]Last prompt:[/b yellow]")
            lines.append(escape(s.last_prompt.strip()[:400]))

        self.query_one("#detail-body", Static).update("\n".join(lines))

    # ── events ──────────────────────────────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_session()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        for s in self.visible_sessions:
            if s.session_id == event.row_key.value:
                self.show_detail(s)
                break

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if item is None:
            return
        self.project_filter = ALL if item.id == "p___all__" else (item.name or ALL)
        self.rebuild_table()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query = event.value
        self.rebuild_table()

    # ── actions ─────────────────────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """←/→ are active only for moving focus between the two panes."""
        if action in ("focus_sessions", "focus_projects"):
            try:
                projects = self.query_one("#projects", ListView)
                table = self.query_one("#table", DataTable)
            except Exception:
                return False
            if action == "focus_sessions":
                return self.focused is projects
            return self.focused is table
        return True

    def action_focus_sessions(self) -> None:
        self.query_one("#table", DataTable).focus()

    def action_focus_projects(self) -> None:
        self.query_one("#projects", ListView).focus()

    def action_open_session(self) -> None:
        """Ask for confirmation and options, then launch the session in Claude."""
        s = self.current
        if s is None:
            self.notify("No session selected", severity="warning")
            return
        if not shutil.which("claude"):
            self.notify("`claude` binary not found in PATH", severity="error")
            return
        if not (s.cwd and os.path.isdir(s.cwd)):
            self.notify(
                f"Session working directory unavailable: {s.cwd or '—'}",
                severity="error",
            )
            return
        self.push_screen(LaunchDialog(s), self._on_launch_decision)

    def _on_launch_decision(self, extra: list[str] | None) -> None:
        if extra is None:
            return
        s = self.current
        claude = shutil.which("claude")
        if s is None or not claude or not (s.cwd and os.path.isdir(s.cwd)):
            return
        cmd = [claude, "--resume", s.session_id, *extra]
        # Suspend the TUI, hand the terminal to Claude, resume on exit.
        with self.suspend():
            subprocess.run(cmd, cwd=s.cwd)
        self.refresh(layout=True)

    async def action_refresh(self) -> None:
        await self.load()

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.add_class("visible")
        search.focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        search.value = ""
        search.remove_class("visible")
        self.query = ""
        self.rebuild_table()
        self.query_one("#table", DataTable).focus()


def main() -> None:
    import sys
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        try:
            from importlib.metadata import version
            print(f"claude-sessions-tui {version('claude-sessions-tui')}")
        except Exception:
            print("claude-sessions-tui 0.1.0")
        return
    if "--help" in args or "-h" in args:
        print(
            "Usage: claude-sessions\n\n"
            "Terminal UI for browsing Claude Code sessions from jsonl files.\n"
            "Reads ~/.claude/projects/*/*.jsonl and lets you resume a session "
            "in Claude.\n\n"
            "Options:\n"
            "  -V, --version   Show version and exit\n"
            "  -h, --help      Show this help and exit"
        )
        return
    SessionsApp().run()


if __name__ == "__main__":
    main()

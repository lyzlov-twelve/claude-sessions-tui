"""Textual TUI for browsing Claude Code sessions."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from rich.markup import escape
from textual import events
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

from .config import CONFIG_FILE, resolve_projects_dir, set_projects_dir
from .parser import Session, scan_sessions

ALL = "__all__"

# Proportional column widths (fractions of the table width), in column order:
# Title, ID, Msgs, Tools, Duration, Activity. They sum to 1.0.
COLUMN_WEIGHTS = (0.40, 0.13, 0.11, 0.11, 0.12, 0.13)
MIN_COLUMN_WIDTH = 4

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


class SessionsTable(DataTable):
    """DataTable that re-lays out its columns whenever its own size changes.

    Reacting to the table's own resize (rather than the app's) means the
    table region already reports its new width — no one-frame lag.
    """

    def on_resize(self, event: events.Resize) -> None:
        relayout = getattr(self.app, "relayout_table", None)
        if callable(relayout):
            relayout()


class SettingsScreen(ModalScreen[str | None]):
    """Edit the sessions folder. Returns the new path on save, None on cancel."""

    CSS = """
    SettingsScreen { align: center middle; }
    #dialog {
        width: 80;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #set-title { text-style: bold; color: $accent; }
    #set-hint { color: $text-muted; padding: 1 0 0 0; }
    #set-input { margin: 1 0; }
    #set-buttons { height: auto; align: center middle; }
    #set-buttons Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current_dir: Path) -> None:
        super().__init__()
        self.current_dir = current_dir

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Settings — sessions folder", id="set-title"),
            Input(value=str(self.current_dir), id="set-input"),
            Label(
                f"Default: ~/.claude/projects\n"
                f"Saved to: {CONFIG_FILE}\n"
                f"Overridable via --projects-dir or $CLAUDE_SESSIONS_DIR.",
                id="set-hint",
            ),
            Horizontal(
                Button("Save", variant="success", id="save"),
                Button("Cancel", variant="error", id="cancel"),
                id="set-buttons",
            ),
            id="dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#set-input", Input).focus()

    def _save(self) -> None:
        value = self.query_one("#set-input", Input).value.strip()
        self.dismiss(value or None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        Binding("s", "settings", "Settings"),
        Binding("r", "refresh", "Refresh"),
        Binding("/", "search", "Search"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "escape", "Quit / clear search"),
        Binding("tab", "focus_next", "Focus", show=False),
    ]

    TITLE = "Claude Code — Sessions"

    def __init__(self, projects_dir: Path | None = None) -> None:
        super().__init__()
        self.projects_dir: Path = projects_dir or resolve_projects_dir()
        self.sessions: list[Session] = []
        self.visible_sessions: list[Session] = []
        self.project_filter: str = ALL
        self.query: str = ""
        self.current: Session | None = None
        self._title_col: object | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sidebar"):
            yield Label("PROJECTS")
            yield ListView(id="projects")
        with Vertical(id="right"):
            yield Input(placeholder="Search by title / prompt / id…", id="search")
            yield SessionsTable(id="table")
            with VerticalScroll(id="detail"):
                yield Static(id="detail-body")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        self._title_col = table.add_columns(
            "Title", "ID", "Msgs", "Tools", "Duration", "Activity",
        )[0]
        await self.load()
        self.call_after_refresh(self.relayout_table)

    # ── data ────────────────────────────────────────────────────────────

    async def load(self) -> None:
        self.sessions = scan_sessions(self.projects_dir)
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
        title_w = self._title_width()
        for s in self.visible_sessions:
            title = self._fit_title(s.title, title_w)
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

    def _resize_columns(self) -> None:
        """Lay out columns proportionally to the table's current width."""
        table = self.query_one("#table", DataTable)
        if not table.columns:
            return
        width = table.scrollable_content_region.width
        if width <= 0:
            return
        n = len(table.columns)
        # each column reserves 2 * cell_padding cells beyond its content width
        avail = width - n * 2 * table.cell_padding
        if avail < n * MIN_COLUMN_WIDTH:
            return
        widths = [max(MIN_COLUMN_WIDTH, int(avail * w)) for w in COLUMN_WEIGHTS]
        # give any rounding leftovers to the Title column
        widths[0] += avail - sum(widths)
        for column, w in zip(table.columns.values(), widths):
            column.auto_width = False
            column.width = max(MIN_COLUMN_WIDTH, w)
        table._require_update_dimensions = True
        table.refresh()

    def _title_width(self) -> int:
        """Current content width of the Title column (fallback before layout)."""
        table = self.query_one("#table", DataTable)
        cols = list(table.columns.values())
        if cols and not cols[0].auto_width and cols[0].width:
            return cols[0].width
        return 30

    @staticmethod
    def _fit_title(title: str, width: int) -> str:
        return title if len(title) <= width else title[: max(1, width - 1)] + "…"

    def _refresh_titles(self) -> None:
        """Re-truncate title cells to the current column width (no cursor reset)."""
        if self._title_col is None:
            return
        table = self.query_one("#table", DataTable)
        width = self._title_width()
        for s in self.visible_sessions:
            try:
                table.update_cell(
                    s.session_id, self._title_col,
                    self._fit_title(s.title, width), update_width=False,
                )
            except Exception:
                pass

    def relayout_table(self) -> None:
        self._resize_columns()
        self._refresh_titles()

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
        # only the search box drives filtering — ignore inputs from modals
        if event.input.id != "search":
            return
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

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.projects_dir), self._on_settings)

    def _on_settings(self, new_dir: str | None) -> None:
        if not new_dir:
            return
        path = Path(os.path.expanduser(new_dir))
        self.projects_dir = path
        set_projects_dir(path)
        self.run_worker(self._reload(), exclusive=True)

    async def _reload(self) -> None:
        await self.load()
        self.call_after_refresh(self.relayout_table)
        ok = self.projects_dir.is_dir()
        self.notify(
            f"Sessions folder: {self.projects_dir}"
            + ("" if ok else "  (folder not found)"),
            severity="information" if ok else "warning",
        )

    def action_escape(self) -> None:
        """Esc clears an active search; otherwise it quits the app."""
        search = self.query_one("#search", Input)
        if "visible" in search.classes or search.value:
            self.action_clear_search()
        else:
            self.exit()

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
            print("claude-sessions-tui 0.1.2")
        return
    if "--help" in args or "-h" in args:
        print(
            "Usage: claude-sessions [--projects-dir PATH]\n\n"
            "Terminal UI for browsing Claude Code sessions from jsonl files.\n"
            "Reads <projects-dir>/*/*.jsonl and lets you resume a session "
            "in Claude.\n\n"
            "Options:\n"
            "  --projects-dir PATH  Folder holding the session jsonl files\n"
            "                       (default ~/.claude/projects; also settable\n"
            "                       in-app with 's' or via $CLAUDE_SESSIONS_DIR)\n"
            "  -V, --version        Show version and exit\n"
            "  -h, --help           Show this help and exit"
        )
        return
    cli_dir: str | None = None
    if "--projects-dir" in args:
        i = args.index("--projects-dir")
        if i + 1 < len(args):
            cli_dir = args[i + 1]
    SessionsApp(resolve_projects_dir(cli_dir)).run()


if __name__ == "__main__":
    main()

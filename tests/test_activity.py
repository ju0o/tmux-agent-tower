from tmux_agent_tower.adapters import resolve_adapter
from tmux_agent_tower.adapters.base import PaneContext
from tmux_agent_tower.adapters.shell import ShellAdapter


def _ctx(fixture_lines, name, title="", command=""):
    return PaneContext(title=title, command=command, lines=tuple(fixture_lines(name)))


# -- Codex ------------------------------------------------------------


def test_codex_activity_prefers_in_progress_calling_line(fixture_lines):
    adapter = resolve_adapter("codex", "", cmdline="codex")
    ctx = _ctx(fixture_lines, "codex-activity-in-progress.txt", command="codex")
    activity = adapter.extract_activity(ctx)
    assert activity is not None
    assert activity.text == "Calling some_tool.example.get_publishable_keys"
    assert activity.confidence == "high"


def test_codex_activity_falls_back_to_last_bullet(fixture_lines):
    adapter = resolve_adapter("codex", "", cmdline="codex")
    ctx = _ctx(fixture_lines, "codex-working.txt", command="codex")
    activity = adapter.extract_activity(ctx)
    assert activity is not None
    assert activity.text == "Called some_tool.example.run_query"
    assert activity.confidence == "medium"


def test_codex_no_bullets_is_no_activity(fixture_lines):
    adapter = resolve_adapter("codex", "", cmdline="codex")
    ctx = _ctx(fixture_lines, "codex-idle.txt", command="codex")
    assert adapter.extract_activity(ctx) is None


def test_codex_finished_turn_summary_bullets_are_not_activity(fixture_lines):
    # Regression: a completed turn leaves its own multi-line summary
    # bullets sitting in scrollback right above the idle "Ask Codex to do
    # anything" prompt. With no "Working (...)" line as evidence, those
    # bullets must not be reported as current activity -- caught live
    # against a real idle Codex pane, not a synthetic fixture.
    adapter = resolve_adapter("codex", "", cmdline="codex")
    ctx = _ctx(fixture_lines, "codex-idle-with-summary-bullets.txt", command="codex")
    assert adapter.extract_activity(ctx) is None


# -- Claude -------------------------------------------------------------


def test_claude_activity_prefers_active_verb(fixture_lines):
    adapter = resolve_adapter("claude", "", cmdline="claude")
    ctx = _ctx(fixture_lines, "claude-working.txt", command="claude")
    activity = adapter.extract_activity(ctx)
    assert activity is not None
    assert activity.text == "Synthesizing"
    assert activity.confidence == "high"


def test_claude_activity_falls_back_to_last_bullet_when_idle(fixture_lines):
    adapter = resolve_adapter("claude", "", cmdline="claude")
    ctx = _ctx(fixture_lines, "claude-idle-with-last-action.txt", command="claude")
    activity = adapter.extract_activity(ctx)
    assert activity is not None
    assert activity.text == "Read src/example.py"
    assert activity.confidence == "low"


def test_claude_no_evidence_is_no_activity(fixture_lines):
    adapter = resolve_adapter("claude", "", cmdline="claude")
    ctx = PaneContext(title="", command="claude", lines=("❯",))
    assert adapter.extract_activity(ctx) is None


# -- Grok ------------------------------------------------------------


def test_grok_activity_extracts_task_description(fixture_lines):
    adapter = resolve_adapter("grok", "", cmdline="grok")
    ctx = _ctx(fixture_lines, "grok-working.txt", command="grok")
    activity = adapter.extract_activity(ctx)
    assert activity is not None
    assert activity.text == "Example background task"
    assert activity.confidence == "medium"


def test_grok_no_task_is_no_activity(fixture_lines):
    adapter = resolve_adapter("grok", "", cmdline="grok")
    ctx = _ctx(fixture_lines, "grok-idle.txt", command="grok")
    assert adapter.extract_activity(ctx) is None


# -- OpenCode -------------------------------------------------------------


def test_opencode_activity_prefers_preparing_line(fixture_lines):
    adapter = resolve_adapter("opencode", "", cmdline="opencode")
    ctx = _ctx(fixture_lines, "opencode-working.txt", command="opencode")
    activity = adapter.extract_activity(ctx)
    assert activity is not None
    assert activity.text == "Preparing write…"
    assert activity.confidence == "high"


def test_opencode_activity_falls_back_to_arrow_tool_call(fixture_lines):
    adapter = resolve_adapter("opencode", "", cmdline="opencode")
    ctx = _ctx(fixture_lines, "opencode-tool-call.txt", command="opencode")
    activity = adapter.extract_activity(ctx)
    assert activity is not None
    assert activity.text == "Read ."
    assert activity.confidence == "medium"


def test_opencode_no_evidence_is_no_activity(fixture_lines):
    adapter = resolve_adapter("opencode", "", cmdline="opencode")
    ctx = _ctx(fixture_lines, "opencode-idle.txt", command="opencode")
    assert adapter.extract_activity(ctx) is None


# -- Cursor / Shell: no verified pattern, must stay honestly silent ------


def test_cursor_has_no_activity_extraction_yet():
    adapter = resolve_adapter("agent", "", cmdline="/x/cursor-agent")
    ctx = PaneContext(title="", command="agent", lines=("Finished running tests", "→ Add a follow-up"))
    # No real capture ever showed an unambiguous in-progress activity line
    # for Cursor (see adapters/cursor.py) -- must not guess one.
    assert adapter.extract_activity(ctx) is None


def test_shell_never_has_activity():
    adapter = ShellAdapter()
    ctx = PaneContext(title="", command="bash", lines=("user@host:~$ ls", "file1  file2"))
    assert adapter.extract_activity(ctx) is None

from tmux_agent_tower.adapters import resolve_adapter
from tmux_agent_tower.adapters.base import PaneContext


def _ctx(fixture_lines, name, title="", command=""):
    return PaneContext(title=title, command=command, lines=tuple(fixture_lines(name)))


def test_codex_working(fixture_lines):
    adapter = resolve_adapter("codex", "Example task title")
    ctx = _ctx(fixture_lines, "codex-working.txt", command="codex")
    assert adapter.classify(ctx).status == "WORKING"


def test_codex_idle(fixture_lines):
    adapter = resolve_adapter("codex", "Example task title")
    ctx = _ctx(fixture_lines, "codex-idle.txt", command="codex")
    assert adapter.classify(ctx).status == "IDLE"


def test_codex_waiting(fixture_lines):
    adapter = resolve_adapter("codex", "Example task title")
    ctx = _ctx(fixture_lines, "codex-waiting.txt", command="codex")
    assert adapter.classify(ctx).status == "WAITING"


def test_codex_first_run_trust_prompt_is_waiting(fixture_lines):
    # Real dogfood finding (2026-09-30 P4 launcher testing): a first-run
    # Codex pane sat at "Trust this folder?" and was misreported as IDLE
    # because no generic waiting pattern matched that exact phrasing.
    adapter = resolve_adapter("codex", "")
    ctx = _ctx(fixture_lines, "codex-trust-prompt.txt", command="codex")
    assert adapter.classify(ctx).status == "WAITING"


def test_codex_spinner_title_is_working(fixture_lines):
    adapter = resolve_adapter("codex", "⠙ doing a thing")
    ctx = PaneContext(title="⠙ doing a thing", command="codex", lines=("",))
    assert adapter.classify(ctx).status == "WORKING"


def test_claude_working(fixture_lines):
    adapter = resolve_adapter("claude", "some title")
    ctx = _ctx(fixture_lines, "claude-working.txt", command="claude")
    assert adapter.classify(ctx).status == "WORKING"


def test_claude_idle_prompt_not_treated_as_working(fixture_lines):
    # Regression: the bottom hint bar contains "esc to interrupt" even when
    # idle -- this must NOT be treated as a working signal on its own.
    adapter = resolve_adapter("claude", "some title")
    ctx = _ctx(fixture_lines, "claude-idle.txt", command="claude")
    assert adapter.classify(ctx).status == "IDLE"


def test_claude_waiting(fixture_lines):
    adapter = resolve_adapter("claude", "some title")
    ctx = _ctx(fixture_lines, "claude-waiting.txt", command="claude")
    assert adapter.classify(ctx).status == "WAITING"


def test_grok_working(fixture_lines):
    adapter = resolve_adapter("grok", "some title")
    ctx = _ctx(fixture_lines, "grok-working.txt", command="grok")
    assert adapter.classify(ctx).status == "WORKING"


def test_grok_idle(fixture_lines):
    adapter = resolve_adapter("grok", "some title")
    ctx = _ctx(fixture_lines, "grok-idle.txt", command="grok")
    assert adapter.classify(ctx).status == "IDLE"


def test_cursor_matches_by_full_cmdline_not_short_command():
    # tmux reports cursor-agent's short command as just "agent"; it must be
    # identified via the full process command line instead (real observed
    # shape: the wrapper binary lives under a "cursor-agent" versions dir).
    cmdline = "/home/user/.local/bin/agent --use-system-ca /home/user/.local/share/cursor-agent/versions/1.0/index.js"
    adapter = resolve_adapter("agent", "My Project Session", cmdline=cmdline)
    assert adapter.name == "Cursor"


def test_cursor_followup_is_idle(fixture_lines):
    adapter = resolve_adapter("agent", "some title", cmdline="/x/cursor-agent")
    ctx = _ctx(fixture_lines, "cursor-idle-followup.txt", command="agent")
    assert adapter.classify(ctx).status == "IDLE"


def test_cursor_waiting(fixture_lines):
    adapter = resolve_adapter("agent", "some title", cmdline="/x/cursor-agent")
    ctx = _ctx(fixture_lines, "cursor-waiting.txt", command="agent")
    assert adapter.classify(ctx).status == "WAITING"


def test_opencode_working(fixture_lines):
    adapter = resolve_adapter("opencode", "some title")
    ctx = _ctx(fixture_lines, "opencode-working.txt", command="opencode")
    assert adapter.classify(ctx).status == "WORKING"


def test_opencode_idle(fixture_lines):
    adapter = resolve_adapter("opencode", "some title")
    ctx = _ctx(fixture_lines, "opencode-idle.txt", command="opencode")
    assert adapter.classify(ctx).status == "IDLE"


def test_opencode_waiting(fixture_lines):
    # NOTE: this fixture is a synthesized generic prompt, not a live
    # OpenCode capture -- its own approval UI was never observed (see
    # adapters/opencode.py's module docstring). This only exercises the
    # generic waiting-pattern fallback that every unmatched adapter shares.
    adapter = resolve_adapter("opencode", "some title")
    ctx = _ctx(fixture_lines, "opencode-waiting.txt", command="opencode")
    assert adapter.classify(ctx).status == "WAITING"


def test_shell_idle_prompt(fixture_lines):
    adapter = resolve_adapter("bash", "")
    ctx = _ctx(fixture_lines, "shell-idle.txt", command="bash")
    assert adapter.classify(ctx).status == "IDLE"


def test_unrecognised_command_falls_back_to_shell_like_adapter():
    adapter = resolve_adapter("some-unknown-binary", "no hints here")
    ctx = PaneContext(title="no hints here", command="some-unknown-binary", lines=("$ ",))
    result = adapter.classify(ctx)
    assert result.status == "IDLE"


def test_empty_content_no_opinion(fixture_lines):
    adapter = resolve_adapter("bash", "")
    ctx = _ctx(fixture_lines, "empty.txt", command="bash")
    assert adapter.classify(ctx).status is None

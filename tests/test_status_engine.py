from tmux_agent_tower.adapters import resolve_adapter
from tmux_agent_tower.adapters.base import PaneContext
from tmux_agent_tower.detection.status import (
    StatusEngine,
    STATUS_WORKING,
    STATUS_WAITING,
    STATUS_IDLE,
    STATUS_UNKNOWN,
    STATUS_DEAD,
)


def _ctx(lines, title="", command="codex"):
    return PaneContext(title=title, command=command, lines=tuple(lines))


def test_dead_pane_is_dead_regardless_of_content():
    engine = StatusEngine()
    adapter = resolve_adapter("codex", "")
    ctx = _ctx(["Working (1s * esc to interrupt)"])
    assert engine.evaluate("p1", True, adapter, ctx) == STATUS_DEAD


def test_first_observation_blank_is_unknown_not_idle():
    # Regression target for the "CHECKING should not exist, but an honest
    # first look at nothing on screen must not become a false IDLE" rule.
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    ctx = _ctx([""], command="bash")
    assert engine.evaluate("p1", False, adapter, ctx) == STATUS_UNKNOWN


def test_unvisited_but_actively_working_pane_reports_working_not_checking():
    # The core bug this project fixes: NEW/SEEN must never leak into status.
    engine = StatusEngine()
    adapter = resolve_adapter("codex", "")
    ctx = _ctx(["Working (2m 0s * esc to interrupt)"])
    # No visit/seen state is passed to evaluate() at all -- status is
    # computed the same whether or not a human has looked at this pane yet.
    assert engine.evaluate("never-seen-pane", False, adapter, ctx) == STATUS_WORKING


def test_output_change_is_working():
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    engine.evaluate("p1", False, adapter, _ctx(["line one"], command="bash"))
    status = engine.evaluate("p1", False, adapter, _ctx(["line one", "line two"], command="bash"))
    assert status == STATUS_WORKING


def test_working_holds_briefly_after_output_pauses():
    engine = StatusEngine(hold_seconds=5.0)
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["a"], command="bash"), now=now)
    engine.evaluate("p1", False, adapter, _ctx(["a", "b"], command="bash"), now=now + 1)
    # Screen stops changing 2s later, well inside the hold window.
    status = engine.evaluate("p1", False, adapter, _ctx(["a", "b"], command="bash"), now=now + 3)
    assert status == STATUS_WORKING


def test_idle_after_hold_window_expires():
    engine = StatusEngine(hold_seconds=2.0)
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["a"], command="bash"), now=now)
    engine.evaluate("p1", False, adapter, _ctx(["a", "b"], command="bash"), now=now + 0.1)
    status = engine.evaluate("p1", False, adapter, _ctx(["a", "b"], command="bash"), now=now + 10)
    assert status == STATUS_IDLE


def test_no_flicker_on_stable_unchanged_screen():
    engine = StatusEngine(hold_seconds=0.01)
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["stable"], command="bash"), now=now)
    for i in range(5):
        status = engine.evaluate(
            "p1", False, adapter, _ctx(["stable"], command="bash"), now=now + 1 + i
        )
        assert status == STATUS_IDLE, "status flickered on an unchanged screen"


def test_generic_waiting_fallback_for_unknown_adapter():
    engine = StatusEngine()
    adapter = resolve_adapter("some-random-tool", "")
    ctx = _ctx(["Do you want to proceed?", "(y/n)"], command="some-random-tool")
    assert engine.evaluate("p1", False, adapter, ctx) == STATUS_WAITING


def test_forget_clears_state():
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    engine.evaluate("p1", False, adapter, _ctx(["a"], command="bash"))
    engine.forget("p1")
    # After forgetting, the next observation is treated as the first again.
    status = engine.evaluate("p1", False, adapter, _ctx([""], command="bash"))
    assert status == STATUS_UNKNOWN

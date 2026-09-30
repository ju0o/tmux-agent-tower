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


# -- status duration ("Task Awareness" v0.2.0) ---------------------------


def test_duration_is_zero_before_any_observation():
    engine = StatusEngine()
    assert engine.duration_seconds("never-observed") == 0.0


def test_duration_starts_at_zero_on_first_observation():
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", False, adapter, _ctx(["stable"], command="bash"), now=now)
    assert engine.duration_seconds("p1", now=now) == 0.0


def test_duration_accumulates_while_status_is_unchanged():
    # A real shell prompt line resolves to IDLE from the very first
    # observation (unlike blank/no-signal content, which is honestly
    # UNKNOWN on the first look) -- avoids an UNKNOWN -> IDLE transition
    # on the second tick that would otherwise reset duration itself.
    engine = StatusEngine(hold_seconds=0.01)
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    ctx = _ctx(["user@host:~$"], command="bash")
    engine.evaluate("p1", False, adapter, ctx, now=now)
    engine.evaluate("p1", False, adapter, ctx, now=now + 5)
    engine.evaluate("p1", False, adapter, ctx, now=now + 12)
    assert engine.duration_seconds("p1", now=now + 12) == 12.0


def test_duration_resets_on_status_transition():
    # hold_seconds tiny so a status change takes effect on the next tick
    # instead of being held over by the WORKING hysteresis window.
    engine = StatusEngine(hold_seconds=0.01)
    adapter = resolve_adapter("codex", "")
    now = 1000.0
    working_ctx = _ctx(["Working (1m • esc to interrupt)"])
    engine.evaluate("p1", False, adapter, working_ctx, now=now)
    engine.evaluate("p1", False, adapter, working_ctx, now=now + 30)
    assert engine.duration_seconds("p1", now=now + 30) == 30.0

    # Transition WORKING -> WAITING: duration must restart from 0, not
    # keep accumulating from when it started WORKING. The content change
    # itself is provisionally read as WORKING for one tick (existing,
    # intentional anti-flicker rule); it settles into WAITING once the
    # same new content is observed unchanged on the following tick.
    waiting_ctx = _ctx(["Allow this command to run?", "1. Yes", "2. No"])
    engine.evaluate("p1", False, adapter, waiting_ctx, now=now + 31)
    status = engine.evaluate("p1", False, adapter, waiting_ctx, now=now + 35)
    assert status == STATUS_WAITING
    assert engine.duration_seconds("p1", now=now + 35) == 0.0
    engine.evaluate("p1", False, adapter, waiting_ctx, now=now + 44)
    assert engine.duration_seconds("p1", now=now + 44) == 9.0


def test_dead_duration_accumulates_across_repeated_observations():
    # Regression: an earlier version called forget() on every single dead
    # observation, which reset status_since to "now" every time -- DEAD
    # duration could never grow past one refresh interval.
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", True, adapter, _ctx([]), now=now)
    engine.evaluate("p1", True, adapter, _ctx([]), now=now + 10)
    engine.evaluate("p1", True, adapter, _ctx([]), now=now + 25)
    assert engine.duration_seconds("p1", now=now + 25) == 25.0


def test_pane_id_reused_after_dead_resets_baseline_not_duration_semantics():
    # A pane_id can be reused by a brand-new process after the old one
    # died; the stale hash/observation baseline must not leak into the
    # new process's first reading (still an honest first-observation).
    engine = StatusEngine()
    adapter = resolve_adapter("bash", "")
    now = 1000.0
    engine.evaluate("p1", True, adapter, _ctx([]), now=now)
    status = engine.evaluate("p1", False, adapter, _ctx(["fresh process output"], command="bash"), now=now + 5)
    assert status == STATUS_UNKNOWN
    assert engine.duration_seconds("p1", now=now + 5) == 0.0

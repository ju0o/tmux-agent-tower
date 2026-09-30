from tmux_agent_tower.tmux import registration


class FakeTmuxOptions:
    """Fakes just enough of tmux's session-option + pane-listing behavior
    to exercise registration.py without a real tmux server."""

    def __init__(self, alive_panes=None):
        self.options = {}
        self.alive_panes = set(alive_panes or [])
        self.focused = []

    def set_session_option(self, session, name, value):
        self.options[(session, name)] = value

    def get_session_option(self, session, name):
        return self.options.get((session, name), "")

    def unset_session_option(self, session, name):
        self.options.pop((session, name), None)

    def pane_exists(self, pane_id):
        return pane_id in self.alive_panes

    def run_tmux(self, args, capture=True):
        if args[0] in ("select-window", "select-pane"):
            self.focused.append(tuple(args))
        return ""


def test_register_and_resolve(monkeypatch):
    fake = FakeTmuxOptions(alive_panes={"%14"})
    monkeypatch.setattr(registration, "capture", fake)

    registration.register("sess", "%14")
    assert registration.resolve_active_pane("sess") == "%14"


def test_resolve_with_no_registration_is_none(monkeypatch):
    fake = FakeTmuxOptions()
    monkeypatch.setattr(registration, "capture", fake)
    assert registration.resolve_active_pane("sess") is None


def test_stale_registration_is_cleared(monkeypatch):
    # Registered pane no longer exists (its Tower exited, tmux restarted, ...).
    fake = FakeTmuxOptions(alive_panes=set())
    monkeypatch.setattr(registration, "capture", fake)

    registration.register("sess", "%14")
    assert registration.resolve_active_pane("sess") is None
    # And the stale value must actually be gone, not just skipped.
    assert fake.get_session_option("sess", registration.PANE_OPTION) == ""


def test_second_tower_becomes_the_active_one(monkeypatch):
    fake = FakeTmuxOptions(alive_panes={"%14", "%19"})
    monkeypatch.setattr(registration, "capture", fake)

    registration.register("sess", "%14")
    registration.register("sess", "%19")
    assert registration.resolve_active_pane("sess") == "%19"


def test_exiting_tower_does_not_erase_a_newer_registration(monkeypatch):
    # Tower A (%14) started, then Tower B (%19) started and became active.
    # A exiting later must not clear B's registration.
    fake = FakeTmuxOptions(alive_panes={"%14", "%19"})
    monkeypatch.setattr(registration, "capture", fake)

    registration.register("sess", "%14")
    registration.register("sess", "%19")
    registration.unregister_if_self("sess", "%14")

    assert registration.resolve_active_pane("sess") == "%19"


def test_exiting_the_active_tower_clears_its_own_registration(monkeypatch):
    fake = FakeTmuxOptions(alive_panes={"%14"})
    monkeypatch.setattr(registration, "capture", fake)

    registration.register("sess", "%14")
    registration.unregister_if_self("sess", "%14")

    assert registration.resolve_active_pane("sess") is None


def test_a_window_named_control_that_isnt_registered_is_never_focused(monkeypatch):
    # No window-name matching anywhere in this module -- only the
    # registered pane_id, verified alive, is ever a focus target.
    fake = FakeTmuxOptions(alive_panes={"%1", "%2"})
    monkeypatch.setattr(registration, "capture", fake)

    registration.register("sess", "%2")
    assert registration.resolve_active_pane("sess") == "%2"


def test_focus_pane_selects_window_then_pane(monkeypatch):
    fake = FakeTmuxOptions()
    monkeypatch.setattr(registration, "capture", fake)

    registration.focus_pane("%14")
    assert fake.focused == [
        ("select-window", "-t", "%14"),
        ("select-pane", "-t", "%14"),
    ]

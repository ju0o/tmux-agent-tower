import subprocess

from tmux_agent_tower import notify


# -- send() backend selection ----------------------------------------------


def test_send_uses_notify_send_when_available(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(notify.tmux_capture, "display_message", lambda msg: calls.append(("fallback", msg)))

    notify.send("hello")

    assert len(calls) == 1
    assert calls[0][0] == "/usr/bin/notify-send"


def test_send_falls_back_to_tmux_display_message(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    monkeypatch.setattr(notify.tmux_capture, "display_message", lambda msg: calls.append(msg))

    notify.send("hello", title="X")

    assert calls == ["X: hello"]


def test_send_falls_back_when_notify_send_itself_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send")

    def fake_run(*a, **k):
        raise OSError("no display")

    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    monkeypatch.setattr(notify.tmux_capture, "display_message", lambda msg: calls.append(msg))

    notify.send("hello", title="X")

    assert calls == ["X: hello"]


# -- NotificationTracker ----------------------------------------------


def test_disabled_tracker_never_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda msg, title=notify.APP_NAME: sent.append(msg))

    tracker = notify.NotificationTracker(enabled=False)
    tracker.observe("p1", "IDLE", "proj")
    result = tracker.observe("p1", "WAITING", "proj")

    assert result is None
    assert sent == []


def test_first_sighting_never_notifies_even_if_already_waiting(monkeypatch):
    # A pane that's already WAITING the very first time Tower sees it must
    # not fire a notification -- there's no real "transition" to report.
    sent = []
    monkeypatch.setattr(notify, "send", lambda msg, title=notify.APP_NAME: sent.append(msg))

    tracker = notify.NotificationTracker(enabled=True, notify_waiting=True)
    result = tracker.observe("p1", "WAITING", "proj")

    assert result is None
    assert sent == []


def test_transition_into_waiting_notifies_once(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda msg, title=notify.APP_NAME: sent.append(msg))

    tracker = notify.NotificationTracker(enabled=True, notify_waiting=True)
    tracker.observe("p1", "WORKING", "proj")
    result = tracker.observe("p1", "WAITING", "proj")

    assert result is not None
    assert len(sent) == 1


def test_staying_in_waiting_does_not_notify_again(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda msg, title=notify.APP_NAME: sent.append(msg))

    tracker = notify.NotificationTracker(enabled=True, notify_waiting=True)
    tracker.observe("p1", "WORKING", "proj")
    tracker.observe("p1", "WAITING", "proj")
    tracker.observe("p1", "WAITING", "proj")
    tracker.observe("p1", "WAITING", "proj")

    assert len(sent) == 1


def test_leaving_and_reentering_waiting_notifies_again(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda msg, title=notify.APP_NAME: sent.append(msg))

    tracker = notify.NotificationTracker(enabled=True, notify_waiting=True)
    tracker.observe("p1", "WORKING", "proj")
    tracker.observe("p1", "WAITING", "proj")
    tracker.observe("p1", "WORKING", "proj")
    tracker.observe("p1", "WAITING", "proj")

    assert len(sent) == 2


def test_working_to_idle_is_not_a_notifiable_transition(monkeypatch):
    # Explicitly not a supported notification kind: WORKING -> IDLE must
    # never be announced as "done" (Tower cannot know that -- see spec).
    sent = []
    monkeypatch.setattr(notify, "send", lambda msg, title=notify.APP_NAME: sent.append(msg))

    tracker = notify.NotificationTracker(enabled=True, notify_waiting=True, notify_dead=True)
    tracker.observe("p1", "WORKING", "proj")
    result = tracker.observe("p1", "IDLE", "proj")

    assert result is None
    assert sent == []


def test_dead_notification_respects_its_own_flag(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda msg, title=notify.APP_NAME: sent.append(msg))

    tracker = notify.NotificationTracker(enabled=True, notify_waiting=True, notify_dead=False)
    tracker.observe("p1", "WORKING", "proj")
    result = tracker.observe("p1", "DEAD", "proj")

    assert result is None
    assert sent == []

    tracker2 = notify.NotificationTracker(enabled=True, notify_dead=True)
    tracker2.observe("p2", "WORKING", "proj")
    result2 = tracker2.observe("p2", "DEAD", "proj")
    assert result2 is not None


def test_forget_clears_tracked_pane():
    tracker = notify.NotificationTracker(enabled=True)
    tracker.observe("p1", "WORKING", "proj")
    tracker.forget("p1")
    # After forgetting, the next observation is treated as a first
    # sighting again (no notification even if it's WAITING).
    assert tracker.observe("p1", "WAITING", "proj") is None

import pytest

from tmux_agent_tower import main, update_manager


def test_normal_tui_start_does_not_check_network(monkeypatch):
    called = []
    monkeypatch.setattr(main, "_warn_if_foreign", lambda: None)
    monkeypatch.setattr(main, "run_ui", lambda: called.append("ui"))
    monkeypatch.setattr(update_manager, "_api_json", lambda *_args: pytest.fail("normal startup must not contact GitHub"))
    main.cli([])
    assert called == ["ui"]


def test_update_flags_route_to_manager_without_entering_tui(monkeypatch):
    captured = {}
    monkeypatch.setattr(main, "runtime_facts", lambda: {"version": "0.3.0rc2", "source": "/tower/__init__.py"})
    monkeypatch.setattr(
        update_manager,
        "run_cli",
        lambda action, **kwargs: captured.update(action=action, **kwargs) or 0,
    )
    with pytest.raises(SystemExit) as result:
        main.cli(["--update", "--dry-run", "--channel", "rc"])
    assert result.value.code == 0
    assert captured["action"] == "update"
    assert captured["dry_run"] is True
    assert captured["requested_channel"] == "rc"


def test_dry_run_requires_an_update_command():
    with pytest.raises(SystemExit) as result:
        main.cli(["--dry-run"])
    assert result.value.code == 2

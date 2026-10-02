import json

from tmux_agent_tower.state.overrides import OverrideStore

SESSION = "main"
PID = "100"


def test_missing_override_returns_none(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    assert store.get_project("%1", SESSION, PID) is None
    assert store.get_agent("%1", SESSION, PID) is None
    assert store.get_title("%1", SESSION, PID) is None


def test_set_and_get_project(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "My Project", SESSION, PID)
    assert store.get_project("%1", SESSION, PID) == "My Project"


def test_set_refuses_a_label_with_no_process_id(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "Claude", SESSION, "")
    assert store.get_agent("%1", SESSION, PID) is None


def test_set_and_get_agent(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "Claude", SESSION, PID)
    assert store.get_agent("%1", SESSION, PID) == "Claude"


def test_custom_agent_free_text(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "Gemini CLI", SESSION, PID)
    assert store.get_agent("%1", SESSION, PID) == "Gemini CLI"


def test_set_and_get_title(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_title("%1", "JuHome dev", SESSION, PID)
    assert store.get_title("%1", SESSION, PID) == "JuHome dev"


def test_fields_are_independent(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "JuHome", SESSION, PID)
    store.set_agent("%1", "Claude", SESSION, PID)
    store.set_title("%1", "JuHome dev", SESSION, PID)

    assert store.get_project("%1", SESSION, PID) == "JuHome"
    assert store.get_agent("%1", SESSION, PID) == "Claude"
    assert store.get_title("%1", SESSION, PID) == "JuHome dev"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "overrides.json"
    store = OverrideStore(path)
    store.set_project("%1", "My Project", SESSION, PID)
    store.set_agent("%1", "Claude", SESSION, PID)

    reopened = OverrideStore(path)
    assert reopened.get_project("%1", SESSION, PID) == "My Project"
    assert reopened.get_agent("%1", SESSION, PID) == "Claude"


def test_reset_clears_only_that_pane(tmp_path):
    path = tmp_path / "overrides.json"
    store = OverrideStore(path)
    store.set_project("%1", "JuHome", SESSION, PID)
    store.set_project("%2", "OtherProject", SESSION, "200")

    store.reset("%1")

    assert store.get_project("%1", SESSION, PID) is None
    assert store.get_project("%2", SESSION, "200") == "OtherProject"


def test_reset_clears_all_fields_for_that_pane(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "JuHome", SESSION, PID)
    store.set_agent("%1", "Claude", SESSION, PID)
    store.set_title("%1", "JuHome dev", SESSION, PID)

    store.reset("%1")

    assert store.get_project("%1", SESSION, PID) is None
    assert store.get_agent("%1", SESSION, PID) is None
    assert store.get_title("%1", SESSION, PID) is None


def test_reset_on_nonexistent_key_is_a_safe_noop(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.reset("%does-not-exist")


def test_old_flat_string_is_kept_but_not_applied(tmp_path):
    # No session or pane pid: applying it would be the stale-label bug.
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": "JuHome", "%2": "OtherProject"}), encoding="utf-8")

    store = OverrideStore(path)
    assert store.get_project("%1", SESSION, PID) is None
    assert store.get_project("%2", SESSION, PID) is None
    assert store.get_agent("%1", SESSION, PID) is None


def test_migrated_project_returns_once_the_user_stamps_this_pane(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": "JuHome"}), encoding="utf-8")

    store = OverrideStore(path)
    store.set_agent("%1", "Claude", SESSION, PID)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["%1"]["project"] == "JuHome"
    assert on_disk["%1"]["agent"] == "Claude"
    assert on_disk["%1"]["session"] == SESSION
    assert on_disk["%1"]["pane_pid"] == PID
    assert store.get_project("%1", SESSION, PID) == "JuHome"
    assert store.get_project("%1", SESSION, "999") is None


def test_malformed_override_file_fails_safe(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = OverrideStore(path)
    assert store.get_project("%1", SESSION, PID) is None
    store.set_project("%1", "Recovered", SESSION, PID)
    assert store.get_project("%1", SESSION, PID) == "Recovered"


def test_malformed_entry_shape_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": ["not", "a", "string", "or", "dict"]}), encoding="utf-8")
    store = OverrideStore(path)
    assert store.get_project("%1", SESSION, PID) is None
    store.set_project("%2", "Fine", SESSION, PID)
    assert store.get_project("%2", SESSION, PID) == "Fine"


def test_stale_pid_is_rejected_and_a_fresh_override_still_wins(tmp_path):
    """The screenshot: JuPortal / CommandCode stuck on a pane now running cursor-agent."""

    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%12", "JuPortal", "main", "111")
    store.set_agent("%12", "CommandCode", "main", "111")

    assert store.get_project("%12", "main", "222") is None
    assert store.get_agent("%12", "main", "222") is None
    assert store.get_agent("%12", "main", "111") == "CommandCode"

    store.set_agent("%12", "CommandCode", "main", "222")
    assert store.get_agent("%12", "main", "222") == "CommandCode"
    assert store.get_agent("%12", "main", "111") is None


def test_drop_if_stale_removes_a_reused_pane_id(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%5", "CommandCode", "0", "111")
    assert store.drop_if_stale("%5", "0", "700") is True
    assert store.get_agent("%5", "0", "111") is None
    assert store.get_agent("%5", "0", "700") is None


def test_drop_if_stale_keeps_a_matching_pane(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%5", "CommandCode", "0", "700")
    assert store.drop_if_stale("%5", "0", "700") is False
    assert store.get_agent("%5", "0", "700") == "CommandCode"

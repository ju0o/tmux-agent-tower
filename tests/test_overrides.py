import json

from tmux_agent_tower.state.overrides import OverrideStore


def test_missing_override_returns_none(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    assert store.get_project("%1") is None
    assert store.get_agent("%1") is None
    assert store.get_title("%1") is None


def test_set_and_get_project(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "My Project")
    assert store.get_project("%1") == "My Project"


def test_set_and_get_agent(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "Claude")
    assert store.get_agent("%1") == "Claude"


def test_custom_agent_free_text(tmp_path):
    # Agent override accepts arbitrary text, not just the known labels --
    # it's display-only metadata (see module docstring), never used to
    # resolve or launch a real command.
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "Gemini CLI")
    assert store.get_agent("%1") == "Gemini CLI"


def test_set_and_get_title(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_title("%1", "JuHome dev")
    assert store.get_title("%1") == "JuHome dev"


def test_fields_are_independent(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "JuHome")
    store.set_agent("%1", "Claude")
    store.set_title("%1", "JuHome dev")

    assert store.get_project("%1") == "JuHome"
    assert store.get_agent("%1") == "Claude"
    assert store.get_title("%1") == "JuHome dev"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "overrides.json"
    store = OverrideStore(path)
    store.set_project("%1", "My Project")
    store.set_agent("%1", "Claude")

    reopened = OverrideStore(path)
    assert reopened.get_project("%1") == "My Project"
    assert reopened.get_agent("%1") == "Claude"


def test_reset_clears_only_that_pane(tmp_path):
    path = tmp_path / "overrides.json"
    store = OverrideStore(path)
    store.set_project("%1", "JuHome")
    store.set_project("%2", "OtherProject")

    store.reset("%1")

    assert store.get_project("%1") is None
    assert store.get_project("%2") == "OtherProject"


def test_reset_clears_all_fields_for_that_pane(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_project("%1", "JuHome")
    store.set_agent("%1", "Claude")
    store.set_title("%1", "JuHome dev")

    store.reset("%1")

    assert store.get_project("%1") is None
    assert store.get_agent("%1") is None
    assert store.get_title("%1") is None


def test_reset_on_nonexistent_key_is_a_safe_noop(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.reset("%does-not-exist")  # must not raise


def test_migrates_old_flat_string_format(tmp_path):
    # v0.1.0/v0.1.1 shape: {pane_id: "project name"}. Existing overrides
    # must never be silently lost when upgrading.
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": "JuHome", "%2": "OtherProject"}), encoding="utf-8")

    store = OverrideStore(path)
    assert store.get_project("%1") == "JuHome"
    assert store.get_project("%2") == "OtherProject"
    # Old entries carry no agent/title override yet.
    assert store.get_agent("%1") is None


def test_migrated_data_is_rewritten_in_new_shape_on_next_write(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": "JuHome"}), encoding="utf-8")

    store = OverrideStore(path)
    store.set_agent("%1", "Claude")

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["%1"] == {"project": "JuHome", "agent": "Claude"}


def test_malformed_override_file_fails_safe(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = OverrideStore(path)
    assert store.get_project("%1") is None
    # Fails safe rather than raising, and can still accept new writes.
    store.set_project("%1", "Recovered")
    assert store.get_project("%1") == "Recovered"


def test_malformed_entry_shape_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"%1": ["not", "a", "string", "or", "dict"]}), encoding="utf-8")
    store = OverrideStore(path)
    assert store.get_project("%1") is None
    # Store still works for other/new keys.
    store.set_project("%2", "Fine")
    assert store.get_project("%2") == "Fine"

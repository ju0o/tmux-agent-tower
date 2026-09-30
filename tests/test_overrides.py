from tmux_agent_tower.state.overrides import OverrideStore


def test_missing_override_returns_none(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    assert store.get("%1") is None


def test_set_and_get(tmp_path):
    store = OverrideStore(tmp_path / "overrides.json")
    store.set("%1", "My Project")
    assert store.get("%1") == "My Project"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "overrides.json"
    OverrideStore(path).set("%1", "My Project")
    assert OverrideStore(path).get("%1") == "My Project"


def test_malformed_override_file_fails_safe(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = OverrideStore(path)
    assert store.get("%1") is None
    # Fails safe rather than raising, and can still accept new writes.
    store.set("%1", "Recovered")
    assert store.get("%1") == "Recovered"

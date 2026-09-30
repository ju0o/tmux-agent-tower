from tmux_agent_tower.launcher import config


def test_default_project_roots_only_includes_existing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "Projects").mkdir()
    roots = config.default_project_roots()
    assert roots == [str(tmp_path / "Projects")]


def test_default_project_roots_empty_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))
    assert config.default_project_roots() == []


def test_load_config_missing_file_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "nope" / "config.toml")
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))
    cfg = config.load_config()
    assert cfg["agents"]["Codex"] == "codex"
    assert isinstance(cfg["project_roots"], list)


def test_load_config_parses_project_roots_and_agent_overrides(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '\n'.join([
            'language = "ko"',
            'project_roots = ["~/A", "/abs/B"]',
            "",
            "[agents]",
            'claude = "claude-beta"',
            'cursor = "cursor-agent-nightly"',
        ]),
        encoding="utf-8",
    )
    import tmux_agent_tower.launcher.config as cfg_module
    old = cfg_module.CONFIG_FILE
    cfg_module.CONFIG_FILE = config_file
    try:
        cfg = cfg_module.load_config()
    finally:
        cfg_module.CONFIG_FILE = old

    assert cfg["language"] == "ko"
    assert cfg["project_roots"][1] == "/abs/B"
    assert cfg["agents"]["Claude"] == "claude-beta"
    assert cfg["agents"]["Cursor"] == "cursor-agent-nightly"
    # Untouched agents keep their defaults.
    assert cfg["agents"]["Codex"] == "codex"


def test_load_config_malformed_file_fails_safe(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("this is not { valid [ toml", encoding="utf-8")

    import tmux_agent_tower.launcher.config as cfg_module
    old = cfg_module.CONFIG_FILE
    cfg_module.CONFIG_FILE = config_file
    try:
        cfg = cfg_module.load_config()
    finally:
        cfg_module.CONFIG_FILE = old

    assert cfg["agents"]["Codex"] == "codex"


def test_resolve_agent_command_missing_binary_is_none():
    agents = {"Codex": "definitely-not-a-real-binary-xyz"}
    assert config.resolve_agent_command("Codex", agents) is None


def test_resolve_agent_command_present_binary(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/" + name)
    agents = {"Codex": "codex --flag"}
    assert config.resolve_agent_command("Codex", agents) == "codex --flag"


def test_resolve_agent_command_unknown_label():
    assert config.resolve_agent_command("Nope", {}) is None

from tmux_agent_tower import i18n


def _use_temp_config(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(i18n, "CONFIG_FILE", config_file)
    return config_file


def test_has_language_configured_false_when_no_file(monkeypatch, tmp_path):
    _use_temp_config(monkeypatch, tmp_path)
    assert i18n.has_language_configured() is False


def test_save_language_creates_file_and_marks_configured(monkeypatch, tmp_path):
    config_file = _use_temp_config(monkeypatch, tmp_path)
    i18n.save_language("en")
    try:
        assert i18n.has_language_configured() is True
        assert i18n.current_language() == "en"
        assert 'language = "en"' in config_file.read_text(encoding="utf-8")
    finally:
        i18n.set_language("ko")


def test_save_language_preserves_other_config_keys(monkeypatch, tmp_path):
    config_file = _use_temp_config(monkeypatch, tmp_path)
    config_file.write_text(
        '\n'.join(['project_roots = ["~/Projects"]', "", "[agents]", 'codex = "codex-beta"']),
        encoding="utf-8",
    )

    i18n.save_language("ko")
    try:
        text = config_file.read_text(encoding="utf-8")
        assert 'language = "ko"' in text
        assert 'project_roots = ["~/Projects"]' in text
        assert 'codex = "codex-beta"' in text
    finally:
        i18n.set_language("ko")


def test_save_language_replaces_existing_line_in_place(monkeypatch, tmp_path):
    config_file = _use_temp_config(monkeypatch, tmp_path)
    config_file.write_text('language = "en"\nproject_roots = ["~/x"]\n', encoding="utf-8")

    i18n.save_language("ko")
    try:
        lines = config_file.read_text(encoding="utf-8").splitlines()
        assert lines.count('language = "ko"') == 1
        assert not any(l.strip() == 'language = "en"' for l in lines)
    finally:
        i18n.set_language("ko")


def test_save_language_invalid_value_is_ignored(monkeypatch, tmp_path):
    config_file = _use_temp_config(monkeypatch, tmp_path)
    i18n.save_language("xx")
    assert i18n.has_language_configured() is False

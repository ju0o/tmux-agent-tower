from tmux_agent_tower import i18n


def test_default_language_is_korean():
    assert i18n.current_language() == "ko"


def test_korean_status_translation():
    assert i18n.t("status.WORKING") == "작업 중"


def test_switch_to_english():
    i18n.set_language("en")
    try:
        assert i18n.t("status.WORKING") == "WORKING"
    finally:
        i18n.set_language("ko")


def test_invalid_language_is_ignored():
    i18n.set_language("ko")
    i18n.set_language("xx-not-a-real-language")
    assert i18n.current_language() == "ko"


def test_unknown_key_falls_back_to_english_then_key_itself():
    assert i18n.t("no.such.key") == "no.such.key"


def test_format_kwargs_are_applied():
    text = i18n.t("footer.selected", host="workstation-a", project="demo")
    assert "workstation-a" in text and "demo" in text

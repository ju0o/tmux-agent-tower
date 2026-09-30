import curses

from tmux_agent_tower.ui.widgets import is_backspace, is_ctrl_c, is_enter, is_escape, matches_letter


def test_is_enter_accepts_str_and_curses_constant():
    assert is_enter("\n")
    assert is_enter("\r")
    assert is_enter(curses.KEY_ENTER)
    assert not is_enter("a")


def test_is_escape_only_matches_esc_string():
    assert is_escape("\x1b")
    assert not is_escape(27)  # get_wch() never returns bare int 27 for Esc
    assert not is_escape("e")


def test_is_ctrl_c_matches_control_char_string():
    assert is_ctrl_c("\x03")
    assert not is_ctrl_c(3)
    assert not is_ctrl_c("c")


def test_is_backspace_accepts_str_and_curses_constant():
    assert is_backspace("\x7f")
    assert is_backspace("\x08")
    assert is_backspace(curses.KEY_BACKSPACE)
    assert not is_backspace("b")


def test_matches_letter_is_case_insensitive():
    assert matches_letter("e", "e")
    assert matches_letter("E", "e")
    assert matches_letter("q", "Q")


def test_matches_letter_rejects_non_matching_or_non_str():
    assert not matches_letter("x", "e")
    assert not matches_letter(curses.KEY_UP, "e")
    assert not matches_letter(-1, "e")


def test_matches_letter_rejects_multibyte_characters():
    # Regression guard for the mojibake bug: a real decoded multi-byte
    # character (e.g. Korean "가") must never accidentally satisfy a
    # single-ASCII-letter hotkey check.
    assert not matches_letter("가", "e")

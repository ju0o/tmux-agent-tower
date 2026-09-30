from tmux_agent_tower.ui import render


def _label(status):
    return {
        "WORKING": "작업 중",
        "WAITING": "입력 대기",
        "IDLE": "대기",
        "UNKNOWN": "확인 불가",
        "DEAD": "종료됨",
    }[status]


# -- format_host_summary -----------------------------------------------


def test_host_summary_hides_zero_counts():
    counts = {"WORKING": 3, "WAITING": 0, "IDLE": 4, "UNKNOWN": 0, "DEAD": 0}
    summary = render.format_host_summary(counts, _label)
    assert "작업 중 3" in summary
    assert "대기 4" in summary
    assert "입력 대기" not in summary
    assert "확인 불가" not in summary
    assert "종료됨" not in summary


def test_host_summary_all_zero_is_empty_string():
    counts = {s: 0 for s in render.STATUS_ORDER}
    assert render.format_host_summary(counts, _label) == ""


def test_host_summary_preserves_status_order():
    counts = {"WORKING": 1, "WAITING": 1, "IDLE": 1, "UNKNOWN": 1, "DEAD": 1}
    summary = render.format_host_summary(counts, _label)
    order = [render.STATUS_ORDER.index(s) for s in render.STATUS_ORDER]
    assert order == sorted(order)  # sanity: STATUS_ORDER itself is fixed
    assert summary.index("작업 중") < summary.index("입력 대기") < summary.index("대기")


# -- looks_meaningful_title ----------------------------------------------


def test_meaningful_title_rejects_empty():
    assert not render.looks_meaningful_title("", "MAINPC")
    assert not render.looks_meaningful_title(None, "MAINPC")


def test_meaningful_title_rejects_unnamed_placeholder():
    assert not render.looks_meaningful_title("(unnamed)", "MAINPC")


def test_meaningful_title_rejects_bare_hostname():
    assert not render.looks_meaningful_title("MAINPC", "MAINPC")
    assert not render.looks_meaningful_title("mainpc", "MAINPC")  # case-insensitive


def test_meaningful_title_accepts_real_title():
    assert render.looks_meaningful_title("JuHome Dev", "MAINPC")


# -- resolve_display_project ----------------------------------------------


def test_project_priority_custom_wins_over_everything():
    name = render.resolve_display_project(
        custom="Custom", git_name="git-name", title="Some Title",
        basename="basename", local_host="MAINPC", no_name_label="(no name)",
    )
    assert name == "Custom"


def test_project_priority_git_name_wins_over_title_and_basename():
    name = render.resolve_display_project(
        custom=None, git_name="my-repo", title="Some Title",
        basename="basename", local_host="MAINPC", no_name_label="(no name)",
    )
    assert name == "my-repo"


def test_project_priority_low_confidence_git_name_is_skipped():
    # A single-letter "git repo name" (unlikely but possible) must not be
    # trusted over a meaningful title.
    name = render.resolve_display_project(
        custom=None, git_name="f", title="JuHome Dev",
        basename="f", local_host="MAINPC", no_name_label="(no name)",
    )
    assert name == "JuHome Dev"


def test_project_priority_meaningful_title_wins_over_low_confidence_basename():
    name = render.resolve_display_project(
        custom=None, git_name=None, title="Prepare P14 direct pilot",
        basename="f", local_host="MAINPC", no_name_label="(no name)",
    )
    assert name == "Prepare P14 direct pilot"


def test_project_priority_basename_used_when_no_better_option():
    name = render.resolve_display_project(
        custom=None, git_name=None, title=None,
        basename="AI-Agent-Marketplace", local_host="MAINPC", no_name_label="(no name)",
    )
    assert name == "AI-Agent-Marketplace"


def test_project_priority_falls_back_to_no_name_label():
    name = render.resolve_display_project(
        custom=None, git_name=None, title="(unnamed)",
        basename="f", local_host="MAINPC", no_name_label="(no name)",
    )
    assert name == "(no name)"


def test_project_priority_generic_basename_is_low_confidence():
    name = render.resolve_display_project(
        custom=None, git_name=None, title=None,
        basename="mnt", local_host="MAINPC", no_name_label="(no name)",
    )
    assert name == "(no name)"


# -- title_secondary_line ----------------------------------------------


def test_title_secondary_line_shown_when_meaningful_and_different():
    line = render.title_secondary_line("JuHome", "JuHome Dev", "MAINPC")
    assert line == "JuHome Dev"


def test_title_secondary_line_hidden_when_duplicate_of_project():
    assert render.title_secondary_line("JuHome", "JuHome", "MAINPC") is None
    assert render.title_secondary_line("JuHome", "juhome", "MAINPC") is None  # case-insensitive


def test_title_secondary_line_hidden_when_not_meaningful():
    assert render.title_secondary_line("JuHome", "", "MAINPC") is None
    assert render.title_secondary_line("JuHome", "MAINPC", "MAINPC") is None


# -- row_matches_filter ----------------------------------------------


def test_filter_empty_matches_everything():
    row = {"project": "JuHome", "agent": "Claude"}
    assert render.row_matches_filter(row, "")


def test_filter_matches_project_case_insensitively():
    row = {"project": "JuHome", "agent": "Claude"}
    assert render.row_matches_filter(row, "juhome")


def test_filter_matches_agent():
    row = {"project": "JuHome", "agent": "Claude"}
    assert render.row_matches_filter(row, "claude")


def test_filter_matches_title_and_path_and_host():
    row = {"project": "x", "title_line": "Marketplace | Codex", "path": "/mnt/f/proj", "host": "ASUS"}
    assert render.row_matches_filter(row, "marketplace")
    assert render.row_matches_filter(row, "/mnt/f")
    assert render.row_matches_filter(row, "asus")


def test_filter_no_match_returns_false():
    row = {"project": "JuHome", "agent": "Claude"}
    assert not render.row_matches_filter(row, "nonexistent")


# -- format_detail_panel ----------------------------------------------


def test_detail_panel_aligns_labels():
    lines = render.format_detail_panel([("프로젝트", "JuHome"), ("에이전트", "Claude")])
    assert lines == ["프로젝트  JuHome", "에이전트  Claude"]


def test_detail_panel_skips_none_values():
    lines = render.format_detail_panel([("A", "x"), ("B", None), ("C", "y")])
    assert lines == ["A  x", "C  y"]


def test_detail_panel_empty_input_is_empty_output():
    assert render.format_detail_panel([]) == []


def test_detail_panel_aligns_mixed_korean_and_english_labels_by_display_width():
    # Regression: plain len() undercounts Korean (double-width) chars, so
    # "프로젝트" (len 4) and "Host" (len 4) looked equal-width to len() but
    # are not on screen -- caught live as visibly misaligned columns.
    lines = render.format_detail_panel([("프로젝트", "JuHome"), ("Host", "MAINPC")])
    # "프로젝트" is 8 display columns, "Host" is 4 -- Host's line needs 4
    # extra spaces of padding to line up with 프로젝트's line.
    assert lines[0] == "프로젝트  JuHome"
    assert lines[1] == "Host      MAINPC"


# -- display_width / truncate_to_width ----------------------------------


def test_display_width_ascii_is_length():
    assert render.display_width("Host") == 4


def test_display_width_korean_is_double():
    assert render.display_width("프로젝트") == 8


def test_display_width_mixed():
    # "Pane " (5 ASCII cols) + "이" (2) + "름" (2) = 9
    assert render.display_width("Pane 이름") == 9


def test_truncate_to_width_ascii_no_truncation_needed():
    assert render.truncate_to_width("short", 20) == "short"


def test_truncate_to_width_ascii_truncates_with_ellipsis():
    result = render.truncate_to_width("a very long project name", 10)
    assert result.endswith("…")
    assert render.display_width(result) <= 10


def test_truncate_to_width_never_splits_a_wide_character():
    # Truncating mid-character would corrupt the string; must stop before it.
    result = render.truncate_to_width("가나다라마바사", 5)
    assert render.display_width(result) <= 5
    # Every character in the result must be a real, complete character
    # from the source string (no partial/garbled output).
    for ch in result.rstrip("…"):
        assert ch in "가나다라마바사"


def test_truncate_to_width_zero_or_negative_is_empty():
    assert render.truncate_to_width("anything", 0) == ""
    assert render.truncate_to_width("anything", -5) == ""


def test_truncate_to_width_never_raises_at_tiny_widths():
    # Part of the resize-to-1x1 regression: width=1 leaves no room even
    # for the ellipsis alone, which must degrade gracefully, not raise.
    for width in (0, 1, 2):
        for text in ("", "a", "가", "한글 프로젝트 이름"):
            result = render.truncate_to_width(text, width)
            assert render.display_width(result) <= max(width, 0)


# -- row_line_count / agent_status_line ----------------------------------


def test_row_line_count_wide_no_title():
    assert render.row_line_count({"title_line": None}, narrow=False) == 1


def test_row_line_count_wide_with_title():
    assert render.row_line_count({"title_line": "JuHome Dev"}, narrow=False) == 2


def test_row_line_count_narrow_no_title():
    assert render.row_line_count({"title_line": None}, narrow=True) == 2


def test_row_line_count_narrow_with_title():
    assert render.row_line_count({"title_line": "JuHome Dev"}, narrow=True) == 3


def test_agent_status_line_format():
    assert render.agent_status_line("Codex", "작업 중") == "Codex  작업 중"


def test_agent_status_line_with_duration():
    assert render.agent_status_line("Codex", "작업 중", "12m") == "Codex  작업 중 · 12m"


def test_row_line_count_with_activity():
    assert render.row_line_count({"title_line": None, "activity_text": "테스트 실행 중"}, narrow=False) == 2


def test_row_line_count_with_title_and_activity():
    row = {"title_line": "JuHome Dev", "activity_text": "테스트 실행 중"}
    assert render.row_line_count(row, narrow=False) == 3


def test_row_line_count_empty_activity_does_not_add_a_line():
    assert render.row_line_count({"title_line": None, "activity_text": None}, narrow=False) == 1
    assert render.row_line_count({"title_line": None, "activity_text": ""}, narrow=False) == 1


# -- format_duration ----------------------------------------------


def test_format_duration_seconds():
    assert render.format_duration(0) == "0s"
    assert render.format_duration(45) == "45s"
    assert render.format_duration(59) == "59s"


def test_format_duration_minutes():
    assert render.format_duration(60) == "1m"
    assert render.format_duration(90) == "1m"
    assert render.format_duration(12 * 60) == "12m"
    assert render.format_duration(59 * 60 + 59) == "59m"


def test_format_duration_hours():
    assert render.format_duration(3600) == "1h"
    assert render.format_duration(3600 + 30 * 60) == "1h 30m"
    assert render.format_duration(7200) == "2h"


def test_format_duration_never_negative():
    assert render.format_duration(-5) == "0s"


# -- sort_by_attention ----------------------------------------------


def test_sort_by_attention_priority_order():
    rows = [
        {"key": "idle", "status": "IDLE"},
        {"key": "working", "status": "WORKING"},
        {"key": "waiting", "status": "WAITING"},
        {"key": "dead", "status": "DEAD"},
        {"key": "unknown", "status": "UNKNOWN"},
    ]
    sorted_rows = render.sort_by_attention(rows)
    assert [r["key"] for r in sorted_rows] == ["waiting", "unknown", "dead", "working", "idle"]


def test_sort_by_attention_is_stable_within_same_status():
    rows = [
        {"key": "a", "status": "WAITING"},
        {"key": "b", "status": "IDLE"},
        {"key": "c", "status": "WAITING"},
    ]
    sorted_rows = render.sort_by_attention(rows)
    # Both WAITING rows come first, in their original relative order.
    assert [r["key"] for r in sorted_rows] == ["a", "c", "b"]


def test_sort_by_attention_does_not_mutate_the_input_list():
    rows = [{"key": "a", "status": "IDLE"}, {"key": "b", "status": "WAITING"}]
    original_order = [r["key"] for r in rows]
    render.sort_by_attention(rows)
    assert [r["key"] for r in rows] == original_order


# -- responsive thresholds ----------------------------------------------


def test_narrow_layout_threshold():
    assert render.use_narrow_layout(50)
    assert not render.use_narrow_layout(120)


def test_detail_panel_visibility_threshold():
    assert not render.should_show_detail_panel(10)
    assert render.should_show_detail_panel(30)


def test_layout_decisions_never_raise_across_extreme_sizes():
    # Regression guard for a real resize stress-test (down to 1x1, up to
    # 300x100): every pure layout-decision function must return a plain
    # bool/int/str for any terminal size Tower could plausibly see, never
    # raise. draw() itself needs a real curses screen to test directly, but
    # these are exactly the decisions it makes every resize.
    for width in (1, 2, 10, 40, 70, 80, 120, 200, 500):
        for height in (1, 2, 5, 10, 18, 24, 40, 100):
            assert isinstance(render.use_narrow_layout(width), bool)
            assert isinstance(render.should_show_detail_panel(height), bool)

    for row in (
        {"title_line": None},
        {"title_line": "x"},
        {"title_line": "매우 긴 한글 제목 줄 테스트입니다"},
    ):
        for narrow in (True, False):
            count = render.row_line_count(row, narrow)
            assert isinstance(count, int) and count >= 1

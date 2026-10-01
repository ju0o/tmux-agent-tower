"""The list is a glance. Enums stay out of the marks a person reads."""

from tmux_agent_tower.i18n import t
from tmux_agent_tower.server import webui
from tmux_agent_tower.ui import render


def _row(**kwargs):
    base = {"status": "IDLE", "attention": "none", "result_state": "none", "project": "타워", "agent": "Codex"}
    base.update(kwargs)
    return base


def test_primary_badge_uses_words_not_enums():
    cases = [
        (_row(status="WORKING"), "●", "작업 중"),
        (_row(attention="approval_required"), "!", "승인 필요"),
        (_row(attention="input_required"), "?", "입력 필요"),
        (_row(result_state="ready"), "✓", "새 결과"),
        (_row(status="IDLE"), "○", "대기"),
        (_row(status="DEAD"), "×", "종료"),
        (_row(status="UNKNOWN"), "◇", "확인 불가"),
        (_row(attention="error"), "!", "오류"),
    ]
    for row, symbol, word in cases:
        mark, key = render.primary_badge(row)
        assert mark == symbol
        assert t(key) == word
        assert t(key) not in {"WORKING", "IDLE", "UNKNOWN", "DEAD", "WAITING"}


def test_detail_can_show_more_than_one_badge():
    row = _row(status="IDLE", attention="approval_required", result_state="ready")
    labels = [t(key) for _symbol, key in render.detail_badges(row)]
    assert labels == ["승인 필요", "새 결과", "대기"]


def test_watch_header_hides_zero_counts():
    rows = [
        _row(status="WORKING"),
        _row(status="WORKING"),
        _row(attention="approval_required"),
        _row(status="IDLE"),
        _row(status="DEAD"),
    ]
    text = render.format_watch_header(render.watch_counts(rows), t)
    assert text == "! 승인 1   ● 작업 2"
    assert "입력" not in text
    assert "결과" not in text
    assert render.format_watch_header(render.watch_counts([_row()]), t) == ""


def test_long_korean_project_truncates_on_display_width():
    name = "한글프로젝트이름아주길게"
    shown = render.truncate_to_width(name, 8)
    assert render.display_width(shown) <= 8
    assert shown.endswith("…")


def test_control_actions_omit_unavailable_writes_and_ignore_navigation():
    plain = render.control_actions(_row(key="%4", pane_id="%4"))
    assert ("A", True) not in plain
    assert ("N", True) not in plain
    assert ("P", True) in plain
    assert ("G", True) in plain
    remote = dict(render.control_actions(_row(key="asus:%1", remote=True, pane_id="%1")))
    assert remote["G"] is False
    assert remote["X"] is False
    approved = dict(render.control_actions(_row(key="%4", approval_known=True, reject_known=True)))
    assert approved["A"] is True and approved["N"] is True


def test_phone_home_is_compact_and_cards_do_not_write():
    html = webui.PAGE_HTML
    assert 'id="counts"' in html
    assert "if (counts.approval)" in html
    assert "if (counts.input)" in html
    assert "if (counts.result)" in html
    assert "if (counts.working)" in html
    render_fn = html.split("function render(")[1].split("function pollList")[0]
    card = render_fn.split('card.addEventListener("click"')[1].split("list.appendChild(card)")[0]
    assert "openDetail(p.key)" in card
    assert "fetch(" not in card
    assert "/api/prompt" not in card
    assert "승인 필요" in html and "◇ 확인 불가" in html
    assert "? 입력 필요" in html
    assert "최신으로" in html and "followLive" in html
    label = render_fn.split('label.className = "window-label"')[1].split("items.forEach")[0]
    assert "addEventListener" not in label

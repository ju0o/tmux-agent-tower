import json

from tmux_agent_tower.adapters.base import PaneContext
from tmux_agent_tower.adapters.grok import GrokAdapter
from tmux_agent_tower.adapters import grok as grok_adapter


def test_local_grok_export_keeps_short_and_long_complete_answers():
    short = "SHORT_GROK_FINAL"
    assert grok_adapter._complete_answer(f"## User\nrequest\n## Assistant\n{short}\n", 0) == short

    body = "GROK_RESULT_START\n" + "\n".join(
        f"ROW_{index:03d} 한글 **Markdown** wrapped line" for index in range(1, 72)
    ) + "\nGROK_RESULT_END"
    recovered = grok_adapter._complete_answer(f"## User\nrequest\n## Assistant\n{body}\n", 0)
    assert recovered == body and len(recovered.splitlines()) == 73


def test_local_grok_uses_only_latest_complete_turn_and_rejects_suffix(monkeypatch):
    transcript = (
        "## User\nfirst\n## Assistant\nOLD\n"
        "## User\nsecond\n## Assistant\nLATEST\n"
    )
    assert grok_adapter._complete_answer(transcript, 1) == "LATEST"
    assert grok_adapter._complete_answer("## Assistant\npartial\n", 0) is None
    monkeypatch.setattr(grok_adapter, "_session_process", lambda _pid: None)
    assert GrokAdapter().extract_result(PaneContext(
        title="Grok", command="grok", lines=("ROW_070", "GROK_RESULT_END"), pane_pid="10",
    )) is None


def test_external_and_tower_sent_turns_use_grok_native_events(tmp_path):
    events = tmp_path / "events.jsonl"
    session_id = "11111111-2222-3333-4444-555555555555"
    events.write_text(
        json.dumps({"type": "turn_started", "session_id": session_id, "turn_number": 0}) + "\n"
        + json.dumps({"type": "turn_ended", "outcome": "success"}) + "\n"
        + json.dumps({"type": "turn_started", "session_id": session_id, "turn_number": 1}) + "\n",
        encoding="utf-8",
    )
    assert grok_adapter._turn_state(str(events), session_id) == (1, False)
    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "turn_ended", "outcome": "success"}) + "\n")
    assert grok_adapter._turn_state(str(events), session_id) == (1, True)

    before = PaneContext(
        title="Grok", command="grok", pane_pid="10",
        turn_watermark=(session_id, 0),
    )
    after = PaneContext(
        title="Grok", command="grok", pane_pid="10",
        turn_watermark=(session_id, 1),
    )
    assert GrokAdapter().confirm_submitted(before, after, "external prompt") is True

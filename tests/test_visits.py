from tmux_agent_tower.state.visits import VisitStore, VISIT_NEW, VISIT_SEEN


def test_unvisited_pane_is_new(tmp_path):
    store = VisitStore(tmp_path)
    assert store.visit_label("session-a", "%1") == VISIT_NEW


def test_mark_seen_persists(tmp_path):
    store = VisitStore(tmp_path)
    store.mark_seen("session-a", "%1")
    assert store.visit_label("session-a", "%1") == VISIT_SEEN

    # A fresh store instance reading the same directory must agree.
    store2 = VisitStore(tmp_path)
    assert store2.visit_label("session-a", "%1") == VISIT_SEEN


def test_seen_is_scoped_per_session(tmp_path):
    store = VisitStore(tmp_path)
    store.mark_seen("session-a", "%1")
    assert store.visit_label("session-b", "%1") == VISIT_NEW


def test_session_name_is_sanitised_for_filesystem(tmp_path):
    store = VisitStore(tmp_path)
    weird_session = "../../etc/passwd"
    store.mark_seen(weird_session, "%1")
    # Must not escape the state directory.
    for path in tmp_path.iterdir():
        assert ".." not in path.name

    assert store.visit_label(weird_session, "%1") == VISIT_SEEN


def test_marking_seen_twice_does_not_duplicate(tmp_path):
    store = VisitStore(tmp_path)
    store.mark_seen("session-a", "%1")
    store.mark_seen("session-a", "%1")
    seen_file = next(tmp_path.glob("seen.session-a"))
    lines = seen_file.read_text(encoding="utf-8").splitlines()
    assert lines.count("%1") == 1

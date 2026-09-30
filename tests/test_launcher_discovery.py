import subprocess

from tmux_agent_tower.launcher.discovery import (
    find_git_projects,
    find_remote_git_projects,
    manual_path_entry,
    load_recent,
    record_recent,
)


def _init_git(path):
    (path / ".git").mkdir()


def test_finds_git_repo_at_root(tmp_path):
    repo = tmp_path / "MyRepo"
    repo.mkdir()
    _init_git(repo)

    projects = find_git_projects([str(tmp_path)])
    assert [p.name for p in projects] == ["MyRepo"]
    assert projects[0].path == str(repo.resolve())


def test_finds_nested_git_repos_within_depth(tmp_path):
    repo = tmp_path / "Category" / "NestedRepo"
    repo.mkdir(parents=True)
    _init_git(repo)

    projects = find_git_projects([str(tmp_path)], max_depth=3)
    assert [p.name for p in projects] == ["NestedRepo"]


def test_does_not_descend_past_max_depth(tmp_path):
    repo = tmp_path / "a" / "b" / "c" / "TooDeepRepo"
    repo.mkdir(parents=True)
    _init_git(repo)

    projects = find_git_projects([str(tmp_path)], max_depth=1)
    assert projects == []


def test_does_not_descend_into_found_repo(tmp_path):
    repo = tmp_path / "Outer"
    repo.mkdir()
    _init_git(repo)
    inner = repo / "vendored" / "InnerRepo"
    inner.mkdir(parents=True)
    _init_git(inner)

    projects = find_git_projects([str(tmp_path)], max_depth=5)
    assert [p.name for p in projects] == ["Outer"]


def test_skips_hidden_and_noise_dirs(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "node_modules").mkdir()
    real = tmp_path / "Real"
    real.mkdir()
    _init_git(real)

    projects = find_git_projects([str(tmp_path)])
    assert [p.name for p in projects] == ["Real"]


def test_nonexistent_root_does_not_raise():
    assert find_git_projects(["/definitely/not/a/real/path/xyz"]) == []


def test_manual_path_entry_flags_non_git():
    entry = manual_path_entry(str(__import__("pathlib").Path("/tmp")))
    assert entry.is_git is False


def test_recent_projects_roundtrip(tmp_path):
    assert load_recent(tmp_path, "MAINPC") == []
    record_recent(tmp_path, "MAINPC", "/a/one")
    record_recent(tmp_path, "MAINPC", "/a/two")
    assert load_recent(tmp_path, "MAINPC") == ["/a/two", "/a/one"]


def test_recent_projects_moves_existing_to_front(tmp_path):
    record_recent(tmp_path, "MAINPC", "/a/one")
    record_recent(tmp_path, "MAINPC", "/a/two")
    record_recent(tmp_path, "MAINPC", "/a/one")
    assert load_recent(tmp_path, "MAINPC") == ["/a/one", "/a/two"]


def test_recent_projects_capped(tmp_path):
    for i in range(30):
        record_recent(tmp_path, "MAINPC", f"/a/{i}")
    recent = load_recent(tmp_path, "MAINPC")
    assert len(recent) == 20
    assert recent[0] == "/a/29"


def test_recent_projects_scoped_per_host(tmp_path):
    record_recent(tmp_path, "MAINPC", "/a/one")
    record_recent(tmp_path, "ASUS", "/a/two")
    assert load_recent(tmp_path, "MAINPC") == ["/a/one"]
    assert load_recent(tmp_path, "ASUS") == ["/a/two"]


def test_find_remote_git_projects_parses_git_dirs(monkeypatch):
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="/home/x/Projects/Alpha/.git\n/home/x/Projects/Beta/.git\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)

    projects = find_remote_git_projects("asus")
    assert [p.name for p in projects] == ["Alpha", "Beta"]
    assert projects[0].path == "/home/x/Projects/Alpha"


def test_find_remote_git_projects_empty_output(monkeypatch):
    fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)
    assert find_remote_git_projects("asus") == []


def test_find_remote_git_projects_nonzero_exit_is_empty(monkeypatch):
    fake_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)
    assert find_remote_git_projects("asus") == []


def test_find_remote_git_projects_timeout_is_empty(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert find_remote_git_projects("asus") == []

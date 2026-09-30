import subprocess

from tmux_agent_tower.detection.project import discover_project


def test_git_repo_uses_toplevel_basename(tmp_path):
    repo = tmp_path / "My-Repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)

    assert discover_project(str(nested)) == "My-Repo"


def test_non_git_path_uses_basename(tmp_path):
    plain = tmp_path / "Not-A-Repo"
    plain.mkdir()
    assert discover_project(str(plain)) == "Not-A-Repo"


def test_empty_path_is_unknown():
    assert discover_project("") == "(unknown)"

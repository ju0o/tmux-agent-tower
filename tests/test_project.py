import subprocess

from tmux_agent_tower.detection.project import discover_project, git_project_name, is_low_confidence_name


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


def test_git_project_name_returns_none_outside_a_repo(tmp_path):
    plain = tmp_path / "Not-A-Repo"
    plain.mkdir()
    assert git_project_name(str(plain)) is None


def test_git_project_name_returns_repo_basename(tmp_path):
    repo = tmp_path / "My-Repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert git_project_name(str(repo)) == "My-Repo"


def test_git_project_name_empty_path_is_none():
    assert git_project_name("") is None


def test_is_low_confidence_name_empty_and_single_char():
    assert is_low_confidence_name(None)
    assert is_low_confidence_name("")
    assert is_low_confidence_name("f")
    assert is_low_confidence_name("c")


def test_is_low_confidence_name_generic_mount_names():
    assert is_low_confidence_name("mnt")
    assert is_low_confidence_name("Home")  # case-insensitive
    assert is_low_confidence_name("tmp")


def test_is_low_confidence_name_real_project_name_is_not_low_confidence():
    assert not is_low_confidence_name("AI-Agent-Marketplace")
    assert not is_low_confidence_name("JuHome")

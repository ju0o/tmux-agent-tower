import inspect
import subprocess

import pytest

from tmux_agent_tower.role_harness import (
    MAX_HARNESS_BYTES,
    HarnessError,
    load_harness,
)
from tmux_agent_tower.state.overrides import ROLE_IDS


def test_all_public_roles_have_readable_markdown_defaults():
    assert set(ROLE_IDS) == {"planner", "builder", "reviewer", "qa", "dogfood", "e2e", "orchestrator"}
    for role_id in ROLE_IDS:
        text = load_harness(role_id)
        assert text.startswith(f"# ")
        assert all(section in text for section in ("## 목적", "## 기대 작업", "## 근거와 보고", "## 한계"))
        assert "\x00" not in text


def test_user_file_overrides_default_and_missing_file_uses_default(tmp_path, monkeypatch):
    import tmux_agent_tower.role_harness as role_harness

    harness_dir = tmp_path / "harnesses"
    harness_dir.mkdir()
    monkeypatch.setattr(role_harness, "DEFAULT_HARNESS_DIR", harness_dir)
    default = load_harness("builder")
    assert load_harness("builder") == default

    (harness_dir / "builder.md").write_text("# 사용자 구현 안내\n\n로컬 내용\n", encoding="utf-8")
    assert load_harness("builder") == "# 사용자 구현 안내\n\n로컬 내용\n"
    assert load_harness("qa") == load_harness("qa", harness_dir=tmp_path / "missing")


def test_invalid_role_and_traversal_are_rejected_before_file_access(tmp_path):
    with pytest.raises(HarnessError, match="지원하지 않는"):
        load_harness("../builder", harness_dir=tmp_path)
    with pytest.raises(HarnessError, match="지원하지 않는"):
        load_harness("Codex", harness_dir=tmp_path)


def test_symlink_override_is_rejected_without_following_target(tmp_path):
    harness_dir = tmp_path / "harnesses"
    harness_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# 외부\n", encoding="utf-8")
    (harness_dir / "builder.md").symlink_to(outside)

    with pytest.raises(HarnessError, match="심볼릭 링크"):
        load_harness("builder", harness_dir=harness_dir)


def test_symlink_harness_directory_is_rejected(tmp_path):
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(actual_dir, target_is_directory=True)

    with pytest.raises(HarnessError, match="디렉터리는 심볼릭 링크"):
        load_harness("builder", harness_dir=linked_dir)


def test_non_regular_override_is_rejected_safely(tmp_path):
    harness_dir = tmp_path / "harnesses"
    harness_dir.mkdir()
    (harness_dir / "builder.md").mkdir()

    with pytest.raises(HarnessError, match="안전하게 열 수 없습니다|일반 파일"):
        load_harness("builder", harness_dir=harness_dir)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\xff", "UTF-8"),
        (b"  \n", "비어 있거나"),
        (b"\x00", "비어 있거나"),
    ],
)
def test_malformed_override_fails_explicitly(tmp_path, payload, message):
    harness_dir = tmp_path / "harnesses"
    harness_dir.mkdir()
    (harness_dir / "builder.md").write_bytes(payload)

    with pytest.raises(HarnessError, match=message):
        load_harness("builder", harness_dir=harness_dir)


def test_oversized_override_fails_explicitly(tmp_path):
    harness_dir = tmp_path / "harnesses"
    harness_dir.mkdir()
    (harness_dir / "builder.md").write_bytes(b"x" * (MAX_HARNESS_BYTES + 1))

    with pytest.raises(HarnessError, match="제한을 초과"):
        load_harness("builder", harness_dir=harness_dir)


def test_missing_default_fails_explicitly(monkeypatch):
    import tmux_agent_tower.role_harness as role_harness

    class MissingResource:
        def joinpath(self, *_parts):
            return self

        def open(self, _mode):
            raise FileNotFoundError

    monkeypatch.setattr(role_harness, "files", lambda _package: MissingResource())
    with pytest.raises(HarnessError, match="기본 역할 안내"):
        role_harness.load_harness("builder", harness_dir=role_harness.DEFAULT_HARNESS_DIR / "missing")


def test_loading_is_agent_independent_and_does_not_call_launchers(tmp_path, monkeypatch):
    from tmux_agent_tower.launcher import spawn

    def forbidden(*_args, **_kwargs):
        raise AssertionError("harness loading must not launch an Agent")

    for name in ("spawn_local", "spawn_into_window"):
        monkeypatch.setattr(spawn, name, forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    assert tuple(inspect.signature(load_harness).parameters) == ("role_id", "harness_dir")
    harness_dir = tmp_path / "harnesses"
    harness_dir.mkdir()
    marker = tmp_path / "executed"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    (harness_dir / "builder.md").write_text(payload, encoding="utf-8")
    builder_text = load_harness("builder", harness_dir=harness_dir)
    assert builder_text == payload
    assert not marker.exists()
    for agent_name in ("Codex", "Claude", "Cursor", "OpenCode"):
        assert agent_name not in inspect.signature(load_harness).parameters
        assert load_harness("builder", harness_dir=harness_dir) == builder_text


def test_loader_does_not_import_agent_or_launcher_modules():
    import tmux_agent_tower.role_harness as role_harness

    assert "launcher" not in role_harness.__dict__
    assert "adapters" not in role_harness.__dict__

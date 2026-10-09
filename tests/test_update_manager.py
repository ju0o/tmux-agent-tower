import json
import subprocess
import sys
from pathlib import Path

import pytest

from tmux_agent_tower import update_manager as updates


SHA_STABLE = "1" * 40
SHA_RC = "2" * 40


def release(tag, *, prerelease=False, draft=False):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": "2026-10-09T00:00:00Z",
    }


def fetcher(records):
    def fetch(path):
        if path == "/releases?per_page=100":
            return records
        if path.startswith("/git/ref/tags/"):
            tag = path.removeprefix("/git/ref/tags/")
            sha = SHA_RC if "rc" in tag else SHA_STABLE
            return {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": sha}}
        raise AssertionError(path)

    return fetch


def test_check_update_selects_track_and_verifies_official_tag():
    records = [release("v0.4.0"), release("v0.4.1-rc1", prerelease=True)]
    stable = updates.check_for_updates("0.3.0", fetch_json=fetcher(records))
    rc = updates.check_for_updates("0.3.0rc2", fetch_json=fetcher(records))
    assert (stable.channel, stable.latest.tag, stable.latest.commit_sha, stable.available) == (
        "stable", "v0.4.0", SHA_STABLE, True
    )
    assert (rc.channel, rc.latest.tag, rc.latest.commit_sha, rc.available) == (
        "rc", "v0.4.1-rc1", SHA_RC, True
    )


def test_annotated_tag_chain_checks_object_identity_and_rejects_cycles():
    tag_a = "a" * 40
    tag_b = "b" * 40

    def fetch(path):
        values = {
            "/git/ref/tags/v0.4.0": {"ref": "refs/tags/v0.4.0", "object": {"type": "tag", "sha": tag_a}},
            f"/git/tags/{tag_a}": {"sha": tag_a, "object": {"type": "tag", "sha": tag_b}},
            f"/git/tags/{tag_b}": {"sha": tag_b, "object": {"type": "commit", "sha": SHA_STABLE}},
        }
        if path == "/releases?per_page=100":
            return [release("v0.4.0")]
        if path not in values:
            raise AssertionError(path)
        return values[path]

    status = updates.check_for_updates("0.3.0", fetch_json=fetch)
    assert status.latest.commit_sha == SHA_STABLE

    def cyclic(path):
        if path == "/git/ref/tags/v0.4.0":
            return {"ref": "refs/tags/v0.4.0", "object": {"type": "tag", "sha": tag_a}}
        if path == "/git/tags/" + tag_a:
            return {"sha": tag_a, "object": {"type": "tag", "sha": tag_a}}
        if path == "/releases?per_page=100":
            return [release("v0.4.0")]
        raise AssertionError(path)

    with pytest.raises(updates.UpdateError) as error:
        updates.check_for_updates("0.3.0", fetch_json=cyclic)
    assert error.value.code == "UNVERIFIED_TAG"


def test_explicit_rc_is_opt_in_from_stable_and_latest_current_is_up_to_date():
    records = [release("v0.3.0"), release("v0.3.1-rc2", prerelease=True)]
    stable = updates.check_for_updates("0.3.0", fetch_json=fetcher(records))
    opt_in = updates.check_for_updates("0.3.0", "rc", fetch_json=fetcher(records))
    assert stable.available is False
    assert opt_in.available is True
    assert opt_in.latest.package_version == "0.3.1rc2"


def test_auto_channel_uses_installed_version_and_never_silently_opts_stable_into_rc():
    records = [
        release("v0.2.2", prerelease=True),
        release("v0.3.1-rc1", prerelease=True),
        release("v0.4.0"),
    ]
    stable = updates.check_for_updates("0.2.2", fetch_json=fetcher(records))
    rc = updates.check_for_updates("0.3.0rc2", fetch_json=fetcher(records))
    assert (stable.channel, stable.latest.tag) == ("stable", "v0.4.0")
    assert (rc.channel, rc.latest.tag) == ("rc", "v0.3.1-rc1")


def test_stable_channel_reports_no_release_when_only_prereleases_exist():
    with pytest.raises(updates.UpdateError) as error:
        updates.check_for_updates("0.3.0", "stable", fetch_json=fetcher([release("v0.3.1-rc1", prerelease=True)]))
    assert error.value.code == "NO_RELEASE"


@pytest.mark.parametrize(
    "records,code",
    [
        ([{"tag_name": "v0.4.0", "prerelease": False, "published_at": "now"}], "INVALID_RELEASE_METADATA"),
        ([release("v0.4.0-rc1", prerelease=False)], "INVALID_RELEASE_METADATA"),
        ([release("v0.4.0-beta1")], "INVALID_RELEASE_TAG"),
    ],
)
def test_invalid_release_metadata_is_rejected(records, code):
    with pytest.raises(updates.UpdateError) as error:
        updates.select_release(records, "stable")
    assert error.value.code == code


def test_offline_is_classified_without_affecting_tower(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(updates.urllib.request, "urlopen", fail)
    with pytest.raises(updates.UpdateError, match="Could not reach GitHub") as error:
        updates._api_json("/releases?per_page=100")
    assert error.value.code == "OFFLINE"


def _editable_install(tmp_path, monkeypatch, *, kind="user", dirty=False):
    source_root = tmp_path / "checkout"
    package = source_root / "src" / "tmux_agent_tower"
    package.mkdir(parents=True)
    source_root.parent.chmod(0o755)
    source_root.chmod(0o755)
    (source_root / ".git").mkdir()
    if kind == "user":
        user_base = tmp_path / "userbase"
        bin_dir = user_base / "bin"
        bin_dir.mkdir(parents=True)
        prefix = base_prefix = sys.prefix
    else:
        venv = source_root / ".venv"
        bin_dir = venv / "bin"
        bin_dir.mkdir(parents=True)
        venv.chmod(0o755)
        user_base = tmp_path / "unused-userbase"
        prefix, base_prefix = str(venv), str(tmp_path / "base-python")
    bin_dir.parent.chmod(0o755)
    bin_dir.chmod(0o755)
    launcher = bin_dir / "tower"
    launcher.write_text(f"#!{sys.executable}\nfrom tmux_agent_tower.main import cli\ncli()\n")
    launcher.chmod(0o755)
    source = package / "__init__.py"
    source.write_text("__version__ = '0.3.0'\n")
    monkeypatch.setattr(updates, "_checkout_dirty", lambda _root: dirty is True if dirty else False)
    path_info = {
        "executable": str(launcher),
        "python": sys.executable,
        "source": str(source),
        "version": "0.3.0",
        "direct_url": json.dumps({"url": source_root.as_uri(), "dir_info": {"editable": True}}),
        "prefix": prefix,
        "base_prefix": base_prefix,
        "user_base": str(user_base),
        "managed_site": "",
    }
    return launcher, source_root, path_info


def test_classify_clean_editable_user_and_venv_fallback(tmp_path, monkeypatch):
    user_launcher, _, user_info = _editable_install(tmp_path / "user", monkeypatch)
    monkeypatch.setattr(updates, "_official_checkout", lambda _root: True)
    user = updates.classify_installation(user_info)
    assert user.managed and user.kind == "editable-user" and user.launcher == user_launcher

    venv_launcher, _, venv_info = _editable_install(tmp_path / "venv", monkeypatch, kind="venv")
    venv = updates.classify_installation(venv_info)
    assert venv.managed and venv.kind == "editable-venv" and venv.launcher == venv_launcher

    user_launcher.parent.chmod(0o777)
    shared = updates.classify_installation(user_info)
    assert not shared.managed and "permissions" in shared.reason

    ancestor_launcher, _, ancestor_info = _editable_install(tmp_path / "ancestor", monkeypatch)
    ancestor_launcher.parent.parent.chmod(0o777)
    ancestor = updates.classify_installation(ancestor_info)
    assert not ancestor.managed and "writable by other users" in ancestor.reason


def test_dirty_developer_checkout_and_unmanaged_wheel_are_refused(tmp_path, monkeypatch):
    _, _, dirty_info = _editable_install(tmp_path / "dirty", monkeypatch, dirty=True)
    dirty = updates.classify_installation(dirty_info)
    assert not dirty.managed and dirty.kind == "dirty-developer-checkout"

    _, _, fork_info = _editable_install(tmp_path / "fork", monkeypatch)
    monkeypatch.setattr(updates, "_official_checkout", lambda _root: False)
    fork = updates.classify_installation(fork_info)
    assert not fork.managed and "official Tower repository" in fork.reason

    _, _, wheel_info = _editable_install(tmp_path / "wheel", monkeypatch)
    wheel_info["direct_url"] = ""
    wheel = updates.classify_installation(wheel_info)
    assert not wheel.managed and wheel.kind == "unmanaged"


@pytest.mark.parametrize(
    "remote,expected",
    [
        ("https://github.com/ju0o/tmux-agent-tower.git", True),
        ("git@github.com:ju0o/tmux-agent-tower.git", True),
        ("https://github.com/someone/tmux-agent-tower.git", False),
        ("https://example-user@github.com/ju0o/tmux-agent-tower.git", False),
        ("https://example.com/ju0o/tmux-agent-tower.git", False),
    ],
)
def test_only_official_clean_editable_repository_is_managed(monkeypatch, tmp_path, remote, expected):
    from types import SimpleNamespace

    monkeypatch.setattr(updates.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=remote))
    assert updates._official_checkout(tmp_path) is expected


def test_old_path_version_is_detected_against_newer_managed_sidecar(tmp_path):
    data = tmp_path / "data"
    installed = data / "versions" / f"0.4.0-{SHA_STABLE[:12]}"
    installed.mkdir(parents=True)
    (installed / "release.json").write_text(json.dumps({
        "version": "0.4.0",
        "commit_sha": SHA_STABLE,
        "launcher_path": "/user/bin/tower",
    }))
    found = updates.newer_managed_release("0.3.0", root=data)
    assert found and found["version"] == "0.4.0"
    assert updates.newer_managed_release("0.4.0", root=data) is None


def test_bootstrap_requires_clean_exact_published_tag(tmp_path):
    root = tmp_path / "bootstrap"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("fixture\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Tower Test", "-c", "user.email=tower@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "tag", "v0.4.0"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    record = release("v0.4.0")
    record_fetch = lambda path: {
        "ref": "refs/tags/v0.4.0",
        "object": {"type": "commit", "sha": commit},
    }
    verified = updates.verify_bootstrap(root, "0.4.0", [record], record_fetch)
    assert verified.commit_sha == commit

    (root / "README.md").write_text("changed after tag\n")
    with pytest.raises(updates.UpdateError) as error:
        updates.verify_bootstrap(root, "0.4.0", [record], record_fetch)
    assert error.value.code == "BOOTSTRAP_DIRTY"


def _release_for_apply():
    return updates.Release("v0.4.0", "0.4.0", False, (0, 4, 0, 1, 0), SHA_STABLE)


def _prepared_apply(tmp_path, monkeypatch):
    launcher, _, path_info = _editable_install(tmp_path / "install", monkeypatch)
    monkeypatch.setattr(updates, "_official_checkout", lambda _root: True)
    installation = updates.classify_installation(path_info)
    monkeypatch.setattr(updates.os, "geteuid", lambda: 1000)
    return launcher, installation


def test_apply_installs_sidecar_then_atomically_switches_launcher(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    old_text = launcher.read_text()
    config = tmp_path / "config.toml"
    cache = tmp_path / "cache"
    config.write_text("user config")
    cache.write_text("user cache")

    def download(_sha, destination):
        destination.write_bytes(b"verified archive fixture")

    def install(_archive, site_dir, _python):
        site_dir.mkdir(parents=True)

    calls = []
    monkeypatch.setattr(
        updates.subprocess,
        "run",
        lambda argv, **_kwargs: pytest.fail(f"updater must not invoke another process: {argv[0]}"),
    )
    result = updates.apply_update(
        _release_for_apply(),
        installation,
        root=tmp_path / "data",
        download_archive=download,
        install_archive=install,
        verify_package=lambda *_args: None,
        run_version=lambda path, _python, _version: calls.append(Path(path)) or True,
    )
    assert len(calls) == 2 and calls[1] == launcher
    assert result["version"] == "0.4.0"
    assert "TOWER_UPDATE_MANAGER_V1" in launcher.read_text()
    assert Path(result["backup"]).read_text() == old_text
    assert config.read_text() == "user config" and cache.read_text() == "user cache"


def test_install_failure_keeps_existing_launcher(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()

    def fail(*_args):
        raise updates.UpdateError("INSTALL_FAILED", "fixture failure")

    with pytest.raises(updates.UpdateError, match="fixture failure"):
        updates.apply_update(
            _release_for_apply(), installation, root=tmp_path / "data",
            download_archive=lambda _sha, path: path.write_bytes(b"fixture"),
            install_archive=fail,
        )
    assert launcher.read_bytes() == before


def test_download_failure_keeps_launcher_and_cleans_staging(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()

    def fail_download(*_args):
        raise updates.UpdateError("OFFLINE", "fixture download failure")

    with pytest.raises(updates.UpdateError, match="fixture download failure"):
        updates.apply_update(
            _release_for_apply(), installation, root=tmp_path / "data",
            download_archive=fail_download,
        )
    assert launcher.read_bytes() == before
    assert list((tmp_path / "data" / "versions").iterdir()) == []


def test_package_version_mismatch_keeps_existing_launcher(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()
    with pytest.raises(updates.UpdateError, match="mismatch"):
        updates.apply_update(
            _release_for_apply(), installation, root=tmp_path / "data",
            download_archive=lambda _sha, path: path.write_bytes(b"fixture"),
            install_archive=lambda _archive, site_dir, _python: site_dir.mkdir(parents=True),
            verify_package=lambda *_args: (_ for _ in ()).throw(updates.UpdateError("VERSION_MISMATCH", "version mismatch")),
        )
    assert launcher.read_bytes() == before


@pytest.mark.parametrize("post_switch", [False, True])
def test_failed_launcher_validation_preserves_or_restores_old_launcher(tmp_path, monkeypatch, post_switch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()
    calls = 0

    def check(_path, _python, _version):
        nonlocal calls
        calls += 1
        return calls == 1 if post_switch else False

    with pytest.raises(updates.UpdateError):
        updates.apply_update(
            _release_for_apply(), installation, root=tmp_path / "data",
            download_archive=lambda _sha, path: path.write_bytes(b"fixture"),
            install_archive=lambda _archive, site_dir, _python: site_dir.mkdir(parents=True),
            verify_package=lambda *_args: None,
            run_version=check,
        )
    assert launcher.read_bytes() == before


def test_post_switch_exception_rolls_back(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()
    calls = 0

    def check(_path, _python, _version):
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        raise OSError("verification runner failed")

    with pytest.raises(updates.UpdateError, match="previous launcher was restored"):
        updates.apply_update(
            _release_for_apply(), installation, root=tmp_path / "data",
            download_archive=lambda _sha, path: path.write_bytes(b"fixture"),
            install_archive=lambda _archive, site_dir, _python: site_dir.mkdir(parents=True),
            verify_package=lambda *_args: None,
            run_version=check,
        )
    assert launcher.read_bytes() == before


def test_post_switch_keyboard_interrupt_rolls_back_and_retry_succeeds(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()
    calls = 0

    def interrupt_after_switch(_path, _python, _version):
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        raise KeyboardInterrupt()

    kwargs = {
        "root": tmp_path / "data",
        "download_archive": lambda _sha, path: path.write_bytes(b"fixture"),
        "install_archive": lambda _archive, site_dir, _python: site_dir.mkdir(parents=True),
        "verify_package": lambda *_args: None,
    }
    with pytest.raises(KeyboardInterrupt):
        updates.apply_update(
            _release_for_apply(), installation, run_version=interrupt_after_switch, **kwargs
        )
    assert launcher.read_bytes() == before

    result = updates.apply_update(
        _release_for_apply(),
        installation,
        root=tmp_path / "data",
        verify_package=lambda *_args: None,
        run_version=lambda *_args: True,
    )
    assert result["version"] == "0.4.0"
    assert "TOWER_UPDATE_MANAGER_V1" in launcher.read_text()


def test_launcher_changed_during_download_is_preserved_and_rejected(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    replacement = b"#!/bin/sh\necho unexpected launcher\n"

    def change_launcher(_sha, archive):
        archive.write_bytes(b"fixture")
        launcher.write_bytes(replacement)

    with pytest.raises(updates.UpdateError) as error:
        updates.apply_update(
            _release_for_apply(), installation, root=tmp_path / "data",
            download_archive=change_launcher,
            install_archive=lambda _archive, site_dir, _python: site_dir.mkdir(parents=True),
            verify_package=lambda *_args: None,
            run_version=lambda *_args: True,
        )
    assert error.value.code == "LAUNCHER_CHANGED"
    assert launcher.read_bytes() == replacement


def test_shared_writable_update_directory_is_rejected(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()
    store = tmp_path / "shared-data"
    store.mkdir(mode=0o777)
    store.chmod(0o777)
    with pytest.raises(updates.UpdateError) as error:
        updates.apply_update(
            _release_for_apply(), installation, root=store,
            download_archive=lambda _sha, path: path.write_bytes(b"fixture"),
            install_archive=lambda _archive, site_dir, _python: site_dir.mkdir(parents=True),
            verify_package=lambda *_args: None,
        )
    assert error.value.code == "UNSAFE_UPDATE_DIRECTORY"
    assert launcher.read_bytes() == before


def test_shared_writable_ancestor_of_update_directory_is_rejected(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()
    shared_parent = tmp_path / "shared-parent"
    shared_parent.mkdir()
    shared_parent.chmod(0o777)
    with pytest.raises(updates.UpdateError) as error:
        updates.apply_update(
            _release_for_apply(), installation, root=shared_parent / "data" / "tower",
            download_archive=lambda _sha, path: path.write_bytes(b"fixture"),
            install_archive=lambda _archive, site_dir, _python: site_dir.mkdir(parents=True),
            verify_package=lambda *_args: None,
        )
    assert error.value.code == "UNSAFE_PATH"
    assert launcher.read_bytes() == before
    assert not (shared_parent / "data").exists()


def test_launcher_replace_failure_keeps_original_launcher(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    before = launcher.read_bytes()
    real_replace = updates.os.replace

    def fail_launcher_replace(source, destination):
        if Path(destination) == launcher and ".tower-update-" in Path(source).name:
            raise OSError("fixture launcher replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(updates.os, "replace", fail_launcher_replace)
    with pytest.raises(OSError, match="replace failure"):
        updates.apply_update(
            _release_for_apply(), installation, root=tmp_path / "data",
            download_archive=lambda _sha, path: path.write_bytes(b"fixture"),
            install_archive=lambda _archive, site_dir, _python: site_dir.mkdir(parents=True),
            verify_package=lambda *_args: None,
            run_version=lambda *_args: True,
        )
    assert launcher.read_bytes() == before


def test_run_cli_refuses_runtime_that_does_not_match_path(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    path_info = {
        "found": True,
        "executable": str(launcher),
        "version": "0.3.0",
        "source": "/old/runtime/tmux_agent_tower/__init__.py",
    }
    monkeypatch.setattr(updates.shutil, "which", lambda _name: str(launcher))
    monkeypatch.setattr(updates, "inspect_path_tower", lambda _path: path_info)
    monkeypatch.setattr(updates, "newer_managed_release", lambda *_args: None)
    monkeypatch.setattr(updates, "classify_installation", lambda *_args, **_kwargs: installation)
    monkeypatch.setattr(
        updates,
        "check_for_updates",
        lambda *_args, **_kwargs: updates.UpdateStatus(
            updates.parse_version("0.3.0"), "stable", _release_for_apply(), True, ()
        ),
    )
    facts = {"version": "0.3.0", "source": "/different/runtime/tmux_agent_tower/__init__.py", "python": sys.executable}
    output = __import__("io").StringIO()
    code = updates.run_cli("update", current_facts=facts, argv0=str(launcher), dry_run=True, output=output)
    assert code == 2
    assert "REFUSED" in output.getvalue()


def test_dry_run_shows_plan_without_creating_state_or_applying(tmp_path, monkeypatch):
    launcher, installation = _prepared_apply(tmp_path, monkeypatch)
    path_info = {
        "found": True,
        "executable": str(launcher),
        "version": "0.3.0",
        "source": "/same/runtime/tmux_agent_tower/__init__.py",
    }
    monkeypatch.setattr(updates.shutil, "which", lambda _name: str(launcher))
    monkeypatch.setattr(updates, "inspect_path_tower", lambda _path: path_info)
    monkeypatch.setattr(updates, "newer_managed_release", lambda *_args: None)
    monkeypatch.setattr(updates, "classify_installation", lambda *_args, **_kwargs: installation)
    monkeypatch.setattr(updates, "data_root", lambda: tmp_path / "data")
    monkeypatch.setattr(
        updates,
        "check_for_updates",
        lambda *_args, **_kwargs: updates.UpdateStatus(
            updates.parse_version("0.3.0"), "stable", _release_for_apply(), True, ()
        ),
    )
    monkeypatch.setattr(updates, "apply_update", lambda *_args, **_kwargs: pytest.fail("dry run must not apply"))
    output = __import__("io").StringIO()
    facts = {"version": "0.3.0", "source": path_info["source"], "python": sys.executable, "mismatch": False}
    code = updates.run_cli("update", current_facts=facts, argv0=str(launcher), dry_run=True, output=output)
    assert code == 0 and "Dry run: no files changed." in output.getvalue()
    assert not (tmp_path / "data").exists()


def test_run_cli_reports_newer_sidecar_for_old_path_runtime(tmp_path, monkeypatch):
    launcher, _, _ = _editable_install(tmp_path / "install", monkeypatch)
    path_info = {
        "found": True,
        "executable": str(launcher),
        "version": "0.3.0",
        "source": "/old/runtime/tmux_agent_tower/__init__.py",
    }
    monkeypatch.setattr(updates.shutil, "which", lambda _name: str(launcher))
    monkeypatch.setattr(updates, "inspect_path_tower", lambda _path: path_info)
    monkeypatch.setattr(updates, "newer_managed_release", lambda *_args: {"version": "0.4.0", "launcher_path": "/user/bin/tower"})
    monkeypatch.setattr(
        updates,
        "check_for_updates",
        lambda *_args, **_kwargs: updates.UpdateStatus(
            updates.parse_version("0.3.0"), "stable", _release_for_apply(), True, ()
        ),
    )
    output = __import__("io").StringIO()
    facts = {"version": "0.3.0", "source": path_info["source"], "python": sys.executable}
    assert updates.run_cli("check", current_facts=facts, argv0=str(launcher), output=output) == 0
    assert "newer managed version 0.4.0 exists" in output.getvalue()

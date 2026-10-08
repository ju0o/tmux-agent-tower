import hashlib
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tmux_agent_tower.detection import sshdest
from tmux_agent_tower.detection.sshdest import ssh_connection_for_pane
from tmux_agent_tower.remote.collector import _REMOTE_GROK_SCRIPT


def _proc_address(address):
    return ipaddress.ip_address(address).packed[::-1].hex().upper()


def _socket_row(local_ip, local_port, peer_ip, peer_port, inode):
    return (
        f"0: {_proc_address(local_ip)}:{local_port:04X} "
        f"{_proc_address(peer_ip)}:{peer_port:04X} 01 "
        f"00000000:00000000 00:00000000 00000000 1000 0 {inode} 1"
    )


def _proc_tcp(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("  sl  local_address rem_address   st\n" + "\n".join(rows) + "\n")


def _local_ssh_proc(root, pid, parent, connection):
    process = root / str(pid)
    (process / "fd").mkdir(parents=True, exist_ok=True)
    (process / "net").mkdir(parents=True, exist_ok=True)
    (process / "cmdline").write_bytes(b"/usr/bin/ssh\0-tt\0remote-host\0grok\0")
    (process / "exe").symlink_to("/usr/bin/ssh")
    (process / "fd" / "3").symlink_to("socket:[12345]")
    client_ip, client_port, server_ip, server_port = connection
    _proc_tcp(process / "net" / "tcp", [
        _socket_row(client_ip, int(client_port), server_ip, int(server_port), 12345),
    ])


def test_local_pane_binds_only_one_direct_ssh_socket(tmp_path):
    connection = ("198.51.100.23", "43123", "203.0.113.31", "22")
    _local_ssh_proc(tmp_path, 20, 10, connection)

    actual = ssh_connection_for_pane(
        "10", "remote-host", ppid_map={"20": "10"}, proc_root=str(tmp_path)
    )

    assert actual == {
        "ssh_client_pid": "20", "client_ip": connection[0], "client_port": connection[1],
        "server_ip": connection[2], "server_port": connection[3],
        "local_socket": connection, "connection_mode": "direct",
    }


def test_wsl_connection_uses_only_one_exact_win_nat_mapping(monkeypatch):
    calls = []
    row = {
        "InternalSourceAddress": "192.0.2.23", "InternalSourcePort": 57120,
        "ExternalSourceAddress": "198.51.100.23", "ExternalSourcePort": 51896,
    }
    monkeypatch.setattr(sshdest.os, "uname", lambda: SimpleNamespace(release="microsoft-standard-WSL2"))
    monkeypatch.setattr(sshdest.shutil, "which", lambda name: "powershell.exe" if name == "powershell.exe" else None)
    monkeypatch.setattr(sshdest.subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(
        returncode=0, stdout=json.dumps(row),
    ))

    assert sshdest._wsl_nat_source("192.0.2.23", 57120) == ("198.51.100.23", 51896)
    assert "Get-NetNatSession" in calls[0][-1]
    assert "InternalSourcePort -eq 57120" in calls[0][-1]

    monkeypatch.setattr(sshdest.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps([row, row]),
    ))
    assert sshdest._wsl_nat_source("192.0.2.23", 57120) is None


def test_local_socket_keeps_nat_and_linux_tuples_separate(tmp_path, monkeypatch):
    local = ("192.0.2.23", "57120", "203.0.113.31", "22")
    _local_ssh_proc(tmp_path, 20, 10, local)
    monkeypatch.setattr(sshdest, "_wsl_nat_source", lambda *_args: ("198.51.100.23", 51896))

    actual = ssh_connection_for_pane(
        "10", "remote-host", ppid_map={"20": "10"}, proc_root=str(tmp_path)
    )

    assert actual["client_ip"] == "198.51.100.23" and actual["client_port"] == "51896"
    assert actual["local_socket"] == local
    assert actual["connection_mode"] == "wsl_nat"


def test_local_binding_fails_closed_for_multiple_or_stale_ssh_processes(tmp_path):
    connection = ("198.51.100.23", "43123", "203.0.113.31", "22")
    _local_ssh_proc(tmp_path, 20, 10, connection)
    (tmp_path / "21").mkdir()
    (tmp_path / "21" / "cmdline").write_bytes(b"/usr/bin/ssh\0remote-host\0grok\0")
    (tmp_path / "21" / "exe").symlink_to("/usr/bin/ssh")

    assert ssh_connection_for_pane(
        "10", "remote-host", ppid_map={"20": "10", "21": "10"}, proc_root=str(tmp_path)
    ) is None
    assert ssh_connection_for_pane(
        "10", "remote-host", ppid_map={"20": "99"}, proc_root=str(tmp_path)
    ) is None


def _remote_proc(root, sessions):
    proc = root / "proc"
    net = proc / "net"
    rows = []
    for item in sessions:
        rows.append(_socket_row(
            item["server_ip"], int(item["server_port"]),
            item["client_ip"], int(item["client_port"]), item["socket_inode"],
        ))
    _proc_tcp(net / "tcp", rows)
    (proc / "net" / "tcp6").parent.mkdir(parents=True, exist_ok=True)
    (proc / "net" / "tcp6").write_text("  sl  local_address rem_address   st\n")
    for index, item in enumerate(sessions):
        sshd = str(90 + index)
        session = str(100 + index)
        grok = str(200 + index)
        expected = (
            f"{item['client_ip']} {item['client_port']} "
            f"{item['server_ip']} {item['server_port']}"
        )
        for pid, comm, parent, env in (
            (sshd, "sshd", "1", b""),
            (session, "bash", sshd, f"SSH_CONNECTION={expected}".encode() + b"\0"),
            (grok, "grok", session, f"SSH_CONNECTION={expected}".encode() + b"\0"),
        ):
            base = proc / pid
            base.mkdir(parents=True, exist_ok=True)
            (base / "stat").write_text(f"{pid} ({comm}) S {parent} 0 0 0 0\n")
            (base / "comm").write_text(comm)
            (base / "environ").write_bytes(env)
        sid = item["session_id"]
        session_dir = root / "grok" / sid
        session_dir.mkdir(parents=True)
        event_path = session_dir / "events.jsonl"
        latest_turn = int(item.get("turn_number", 0))
        events = []
        for turn in range(latest_turn + 1):
            events.append(json.dumps({"type": "turn_started", "session_id": sid, "turn_number": turn}))
            if turn < latest_turn or item.get("turn_complete", True):
                events.append(json.dumps({"type": "turn_ended", "outcome": "success"}))
        event_path.write_text("\n".join(events) + "\n")
        exe = root / f"grok-export-{index}"
        output = item.get("transcript", "")
        status = item.get("exit_code", 0)
        exe.write_text(
            "#!/usr/bin/env python3\n"
            f"import sys\nsys.stdout.write({output!r})\nsys.exit({status})\n"
        )
        exe.chmod(0o700)
        (proc / grok / "fd").mkdir(parents=True)
        (proc / grok / "fd" / "3").symlink_to(event_path)
        (proc / grok / "exe").symlink_to(exe)
    return proc


def _run_remote_provider(proc, connection):
    result = subprocess.run(
        [sys.executable, "-c", _REMOTE_GROK_SCRIPT, *connection, str(proc)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    marker = "__TOWER_GROK_RESULT__"
    line = next((line for line in result.stdout.splitlines() if line.startswith(marker)), "")
    assert result.returncode == 0 and line
    return json.loads(line[len(marker):])


def _session(client_port, body):
    sid = f"12345678-1234-1234-1234-{client_port:012d}"
    transcript = f"## User\nrequest\n## Assistant\n{body}\n"
    return {
        "client_ip": "198.51.100.23", "client_port": str(client_port),
        "server_ip": "203.0.113.31", "server_port": "22",
        "socket_inode": 50000 + client_port, "session_id": sid, "transcript": transcript,
    }


def test_remote_provider_returns_only_the_exact_latest_complete_export(tmp_path):
    long_body = "\n".join([
        "REMOTE_GROK_LONG_BEGIN",
        *[f"| {index:02d} | 한글 wrapped **Markdown** body |" for index in range(72)],
        "REMOTE_GROK_LONG_FINAL",
    ])
    item = _session(43123, long_body)
    proc = _remote_proc(tmp_path, [item])

    result = _run_remote_provider(proc, (
        item["client_ip"], item["client_port"], item["server_ip"], item["server_port"],
    ))

    assert result["status"] == "ok"
    assert result["remote_session_pid"] == "100"
    assert result["remote_sshd_pid"] == "90"
    assert result["grok_pid"] == "200"
    assert result["session_id"] == item["session_id"]
    assert result["turn_complete"] is True and result["body_complete"] is True
    assert result["text"] == long_body
    assert result["native_length"] == len(long_body)
    assert result["native_sha256"] == hashlib.sha256(long_body.encode()).hexdigest()


def test_two_remote_sessions_are_selected_only_by_their_connection(tmp_path):
    session_a = _session(43123, "SESSION_A_ONLY")
    session_b = _session(43124, "SESSION_B_ONLY")
    proc = _remote_proc(tmp_path, [session_a, session_b])

    a = _run_remote_provider(proc, (
        session_a["client_ip"], session_a["client_port"], session_a["server_ip"], session_a["server_port"],
    ))
    b = _run_remote_provider(proc, (
        session_b["client_ip"], session_b["client_port"], session_b["server_ip"], session_b["server_port"],
    ))

    assert a["status"] == b["status"] == "ok"
    assert a["text"] == "SESSION_A_ONLY"
    assert b["text"] == "SESSION_B_ONLY"
    assert a["session_id"] == session_a["session_id"]
    assert b["session_id"] == session_b["session_id"]
    assert a["session_id"] != b["session_id"]
    assert a["grok_pid"] != b["grok_pid"]


def test_same_connection_with_two_grok_sessions_is_unbound(tmp_path):
    item = _session(43123, "SHOULD_NOT_BE_SELECTED")
    duplicate = {
        **item,
        "socket_inode": 50002,
        "session_id": "87654321-4321-4321-4321-000000043123",
    }
    proc = _remote_proc(tmp_path, [item, duplicate])

    result = _run_remote_provider(proc, (
        item["client_ip"], item["client_port"], item["server_ip"], item["server_port"],
    ))

    assert result["status"] == "REMOTE_PROVIDER_UNBOUND"
    assert "text" not in result


def test_disconnected_remote_socket_is_unbound(tmp_path):
    proc = _remote_proc(tmp_path, [])
    result = _run_remote_provider(proc, (
        "198.51.100.23", "43123", "203.0.113.31", "22",
    ))

    assert result["status"] == "REMOTE_PROVIDER_UNBOUND"
    assert "text" not in result


def test_remote_export_failure_is_not_a_complete_result(tmp_path):
    item = {**_session(43123, "unavailable"), "exit_code": 1}
    proc = _remote_proc(tmp_path, [item])

    result = _run_remote_provider(proc, (
        item["client_ip"], item["client_port"], item["server_ip"], item["server_port"],
    ))

    assert result["status"] == "REMOTE_PROVIDER_UNAVAILABLE"
    assert result["turn_complete"] is True and result["body_complete"] is False
    assert "text" not in result


def test_remote_provider_uses_only_the_newest_ended_turn(tmp_path):
    item = _session(43123, "old answer")
    latest = "LATEST_ONLY\n한글 **Markdown**"
    item["turn_number"] = 1
    item["transcript"] = (
        "## User\nold request\n## Assistant\nold answer\n"
        "## User\nnew request\n## Assistant\n" + latest + "\n"
    )
    proc = _remote_proc(tmp_path, [item])

    result = _run_remote_provider(proc, (
        item["client_ip"], item["client_port"], item["server_ip"], item["server_port"],
    ))

    assert result["status"] == "ok"
    assert result["turn_number"] == 1
    assert result["text"] == latest


def test_remote_provider_requires_turn_and_body_completeness(tmp_path):
    item = {**_session(43123, "incomplete answer"), "turn_complete": False}
    proc = _remote_proc(tmp_path, [item])

    result = _run_remote_provider(proc, (
        item["client_ip"], item["client_port"], item["server_ip"], item["server_port"],
    ))

    assert result["status"] == "REMOTE_RESULT_INCOMPLETE"
    assert result["turn_complete"] is False and result["body_complete"] is False
    assert "text" not in result

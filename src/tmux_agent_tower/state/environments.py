"""SSH environment profiles stored in the legacy remote-hosts file.

The file format stays ``ssh_target[:display_name]`` so existing profiles
are their own migration. Transport is always SSH in this release.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List

_TARGET = re.compile(r"^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}$")


def valid_ssh_target(value: str) -> bool:
    return bool(_TARGET.fullmatch((value or "").strip()))


def _profile(raw: str) -> Dict[str, str]:
    target, sep, display = raw.partition(":")
    target = target.strip()
    name = (display.strip() if sep else target)
    return {
        "alias": target,
        "name": name,
        "display_name": name,
        "transport": "ssh",
        "ssh_target": target,
    }


def load_profiles(path: Path) -> List[Dict[str, str]]:
    try:
        return [
            _profile(line.strip())
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except FileNotFoundError:
        return []


def _read_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _write_lines(path: Path, lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + ("\n" if lines else "")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def add_profile(path: Path, display_name: str, ssh_target: str) -> None:
    name = (display_name or "").strip()
    target = (ssh_target or "").strip()
    if not name or len(name) > 80 or any(ord(ch) < 32 for ch in name):
        raise ValueError("환경 이름을 확인해 주세요.")
    if not valid_ssh_target(target):
        raise ValueError("SSH 대상은 별칭, 사용자@호스트, 호스트 이름 또는 IP 주소로 입력해 주세요.")
    lines = _read_lines(path)
    if any(_profile(line.strip())["alias"].casefold() == target.casefold()
           for line in lines if line.strip() and not line.lstrip().startswith("#")):
        raise ValueError("이미 등록된 SSH 대상입니다.")
    lines.append(f"{target}:{name}")
    _write_lines(path, lines)


def rename_profile(path: Path, ssh_target: str, display_name: str) -> None:
    name = (display_name or "").strip()
    if not name or len(name) > 80 or any(ord(ch) < 32 for ch in name):
        raise ValueError("환경 이름을 확인해 주세요.")
    lines = _read_lines(path)
    for index, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith("#") and _profile(line.strip())["alias"] == ssh_target:
            lines[index] = f"{ssh_target}:{name}"
            _write_lines(path, lines)
            return
    raise ValueError("실행 환경을 찾을 수 없습니다.")


def remove_profile(path: Path, ssh_target: str) -> None:
    lines = _read_lines(path)
    kept = [line for line in lines if not (
        line.strip() and not line.lstrip().startswith("#")
        and _profile(line.strip())["alias"] == ssh_target
    )]
    if len(kept) == len(lines):
        raise ValueError("실행 환경을 찾을 수 없습니다.")
    _write_lines(path, kept)


def ssh_target_available(ssh_target: str, timeout: float = 4.0) -> bool:
    if not valid_ssh_target(ssh_target):
        return False
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", ssh_target, "true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

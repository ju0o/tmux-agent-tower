"""Temporary directories for workspace tests.

Nothing here reads a home directory or a fixed Projects folder. The
three-level tree exists only under the directory the caller passes,
usually pytest's tmp_path.
"""

import subprocess
from pathlib import Path


def make_three_level_git_workspace(base: Path) -> tuple[Path, Path, Path]:
    """Create ``tree-root/group/sample-repo`` and initialize the leaf with git.

    Returns resolved ``(tree_root, group, repo)``. ``repo`` is a real git
    repository so browse and discovery treat it as Git, not a plain folder.
    """

    tree_root = base / "tree-root"
    group = tree_root / "group"
    repo = group / "sample-repo"
    repo.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return tree_root.resolve(), group.resolve(), repo.resolve()

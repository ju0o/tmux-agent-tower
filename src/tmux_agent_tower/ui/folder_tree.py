"""Build the user-work hierarchy from Tower's shared folder projection."""

from __future__ import annotations

from typing import Dict, Iterable, List

from ..i18n import t
from ..state.folders import FolderStore, window_ref
from . import render
from .work_groups import aggregate, compact_summary, summary_text


def infer_window_assets(windows: Iterable[dict], tasks: Iterable[dict], local_host: str) -> List[dict]:
    tasks_by_window: Dict[str, List[dict]] = {}
    for task in tasks:
        ref = task.get("window_ref")
        if ref:
            tasks_by_window.setdefault(ref, []).append(task)
    assets = []
    for source in windows:
        asset = dict(source)
        members = tasks_by_window.get(asset.get("window_ref"), [])
        title = str(asset.get("window_name") or "").strip()
        if render.looks_meaningful_title(title, local_host):
            name = title
        else:
            projects = list(dict.fromkeys(
                str(row.get("project") or "").strip() for row in members
                if row.get("project") and row.get("project") != t("project.no_name")
            ))
            active = next((row for row in members if row.get("pane_active")), None)
            name = projects[0] if len(projects) == 1 else (
                str((active or (members[0] if members else {})).get("display_name") or "").strip()
            )
        asset["inferred_name"] = name or t("folder.terminal")
        assets.append(asset)
    return assets


def visible_windows(windows: Iterable[dict], rows: Iterable[dict], own_window_id: str = "") -> List[dict]:
    """Hide windows occupied only by Tower, while keeping mixed user windows."""
    windows = list(windows)
    rows = list(rows)
    user_refs = {row.get("window_ref") for row in rows
                 if row.get("pane_id") and not row.get("tower_runtime") and row.get("window_ref")}
    runtime_refs = {row.get("window_ref") for row in rows
                    if row.get("pane_id") and row.get("tower_runtime") and row.get("window_ref")}
    own_refs = {window.get("window_ref") for window in windows
                if own_window_id and str(window.get("window_id") or "") == own_window_id}
    hidden = (runtime_refs | own_refs) - user_refs
    return [window for window in windows if window.get("window_ref") not in hidden]


def build_rows(store: FolderStore, windows: Iterable[dict], tasks: Iterable[dict],
               filter_text: str = "", collapsed_other: bool = False) -> List[dict]:
    windows = list(windows)
    tasks = [row for row in tasks if row.get("pane_id")]
    workspace = store.workspace(windows, tasks)
    source_by_ref = {str(row.get("window_ref") or window_ref(row)): row for row in windows}
    task_by_ref = {
        str(row.get("target_id") or row.get("key")): row for row in tasks
        if row.get("target_id") or row.get("key")
    }
    query = filter_text.casefold().strip()
    out: List[dict] = []

    def filtered_tasks(window: dict, parent_match: bool) -> List[dict]:
        members = [task_by_ref[target] for target in window.get("task_target_ids", []) if target in task_by_ref]
        return members if not query or parent_match else [row for row in members if render.row_matches_filter(row, query)]

    def append_window(window: dict, guide: str, parent_match: bool = False) -> bool:
        name = window["display_name"]
        source = source_by_ref.get(window["window_ref"], {})
        window_match = bool(query and query in name.casefold())
        members = filtered_tasks(window, parent_match or window_match)
        if query and not parent_match and not window_match and not members:
            return False
        row = {
            "key": "windowasset:" + window["window_ref"], "kind": "window_asset",
            "window_ref": window["window_ref"], "window_id": source.get("window_id") or "",
            "session": source.get("session") or "", "window_name": source.get("window_name") or "",
            "display_name": name, "task_target_ids": list(window.get("task_target_ids") or []),
            "summary_counts": window.get("summary_counts") or {},
            "summary": summary_text(window.get("summary_counts") or {}),
            "summary_compact": compact_summary(window.get("summary_counts") or {}),
            "status": "UNKNOWN" if window.get("stale") else "IDLE",
            "collapsed": bool(window.get("collapsed")), "stale": bool(window.get("stale")),
            "folder_id": window.get("folder_id"), "guide": guide,
            "remote": bool(source.get("remote")), "host": source.get("host") or "",
        }
        out.append(row)
        if row["collapsed"] and not query:
            return True
        for index, member in enumerate(members):
            task = dict(member)
            task["kind"] = "pane"
            task["window_ref"] = window["window_ref"]
            task["guide"] = guide + ("   └─ " if index == len(members) - 1 else "   ├─ ")
            out.append(task)
        return True

    for folder in workspace["folders"]:
        name = folder["display_name"]
        folder_match = bool(query and query in name.casefold())
        matched_windows = []
        for window in folder["windows"]:
            if (not query or folder_match
                    or filtered_tasks(window, False) or query in window["display_name"].casefold()):
                matched_windows.append(window)
        if query and not folder_match:
            matched_windows = [window for window in matched_windows
                               if query in window["display_name"].casefold()
                               or filtered_tasks(window, False)]
        if query and not folder_match and not matched_windows:
            continue
        folder_row = {
            "key": "folder:" + folder["folder_id"], "kind": "folder",
            "folder_id": folder["folder_id"], "display_name": name,
            "summary_counts": folder["summary_counts"],
            "summary": summary_text(folder["summary_counts"]),
            "summary_compact": compact_summary(folder["summary_counts"]),
            "collapsed": folder["collapsed"] and not query,
            "guide": "▶ " if folder["collapsed"] and not query else "▼ ",
            "window_refs": list(folder["window_refs"]), "stale": False,
        }
        out.append(folder_row)
        if folder_row["collapsed"]:
            continue
        for index, window in enumerate(matched_windows):
            branch = "   " + ("└─ " if index == len(matched_windows) - 1 else "├─ ")
            append_window(window, branch, folder_match)

    unfiled = workspace["unfiled_windows"]
    other_match = bool(query and query in t("folder.other").casefold())
    matched_unfiled = [window for window in unfiled
                       if (not query or other_match or query in window["display_name"].casefold()
                           or filtered_tasks(window, False))]
    if matched_unfiled:
        counts = aggregate_window_list(matched_unfiled)
        collapsed = collapsed_other and not query
        out.append({
            "key": "folder:__unfiled__", "kind": "other_section", "folder_id": "__unfiled__",
            "display_name": t("folder.other"), "summary_counts": counts,
            "summary": summary_text(counts), "summary_compact": compact_summary(counts),
            "collapsed": collapsed, "guide": "▶ " if collapsed else "▼ ",
            "window_refs": [window["window_ref"] for window in unfiled],
        })
        if not collapsed:
            for index, window in enumerate(matched_unfiled):
                branch = "   " + ("└─ " if index == len(matched_unfiled) - 1 else "├─ ")
                append_window(window, branch)
    return out


def aggregate_window_list(windows: Iterable[dict]) -> dict:
    keys = ("error", "attention", "working", "result", "idle", "unavailable")
    total = {key: 0 for key in keys}
    for window in windows:
        for key in keys:
            total[key] += (window.get("summary_counts") or {}).get(key, 0)
    return total

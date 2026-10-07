"""Build the default logical work list from saved groups and live panes."""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Set

from ..i18n import t
from ..state.work_groups import aggregate
from . import render


def summary_text(counts: Dict[str, int]) -> str:
    labels = (
        ("error", "!", "group.error"),
        ("attention", "!", "group.attention"),
        ("working", "●", "group.working"),
        ("result", "✓", "group.result"),
        ("idle", "○", "group.idle"),
        ("unavailable", "◇", "group.unavailable"),
    )
    return " · ".join(f'{symbol} {counts[key]} {t(label)}' for key, symbol, label in labels if counts.get(key))


def compact_summary(counts: Dict[str, int]) -> str:
    symbols = (("error", "✕"), ("attention", "!"), ("working", "●"), ("result", "✓"), ("idle", "○"), ("unavailable", "◇"))
    return "  ".join(f'{symbol}{counts[key]}' for key, symbol in symbols if counts.get(key)) or t("group.empty")


def build_rows(groups: Sequence[dict], panes: Iterable[dict], collapsed: Set[str], filter_text: str = "") -> List[dict]:
    panes = list(panes)
    by_target = {str(row.get("target_id")): row for row in panes if row.get("target_id")}
    membership = {target for group in groups for target in group.get("member_target_ids", [])}
    output: List[dict] = []
    query = filter_text.casefold().strip()

    for group in groups:
        group_id = group["group_id"]
        name = group["display_name"]
        ordered = list(group.get("member_order") or group.get("member_target_ids") or [])
        live = [by_target[target] for target in ordered if target in by_target]
        labels = group.get("member_labels") or {}
        stale_ids = [target for target in ordered if target not in by_target]
        stale = [
            {"key": f"stale:{target}", "target_id": target, "kind": "work_group_stale",
             "display_name": labels.get(target) or t("group.stale_member"), "status": "UNKNOWN",
             "attention": "none", "result_state": "none", "host": "", "stale": True}
            for target in stale_ids
        ]
        all_members = live + stale
        counts = aggregate(all_members)
        binding = group.get("project_binding") or {}
        group_match = bool(query and (query in name.casefold() or query in str(binding.get("name") or "").casefold()))
        matches = [row for row in all_members if not query or group_match or render.row_matches_filter(row, query)]
        if query and not matches and not group_match:
            continue
        visible_members = all_members if not query or group_match else matches
        group_row = {
            "key": f"group:{group_id}", "kind": "work_group", "group_id": group_id,
            "display_name": name, "project": binding.get("name") or "",
            "summary": summary_text(counts), "summary_compact": compact_summary(counts), "summary_counts": counts,
            "member_count": len(group.get("member_target_ids", [])),
            "layout": group.get("layout") or "focus",
            "layout_slots": dict(group.get("layout_slots") or {}),
            "collapsed": group_id in collapsed and not query,
            "status": "UNKNOWN", "attention": "none", "result_state": "none",
            "guide": "▶ " if group_id in collapsed and not query else "▼ ",
        }
        output.append(group_row)
        if group_row["collapsed"]:
            continue
        for index, row in enumerate(visible_members):
            member = dict(row)
            member["work_group_id"] = group_id
            member["work_group_name"] = name
            member["group_member"] = True
            member["guide"] = "  " + ("└─ " if index == len(visible_members) - 1 else "├─ ")
            output.append(member)

    output.extend(
        row for row in panes
        if (not row.get("target_id") or row.get("target_id") not in membership)
        and render.row_matches_filter(row, query)
    )
    return output

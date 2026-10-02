# Tmux Agent Tower — PM Handoff

## Current
- Branch: `feat/tower-remote`
- HEAD: tip of this branch. Result recovery is `a764010`. SSH clipboard is `20d427a`.
- Worktree: clean
- Tests: 640 passed, 1 skipped, 0 failed

## Last Task
- Y on an ASUS SSH session into MAINPC, then WSL, then `tmux attach`, said there was no result. After recovery, the copy still did not reach the laptop clipboard.
- Root cause: the live Tower process is still public `main`, which has no result copy. On this branch the second cause is the 2s refresh, which reads 30 lines. A finished answer can sit above that window, so Y reports that there is no result.
- Y and S, only when the in-memory tracker is empty, read at most 800 lines and 256KB and keep the newest finished turn. A WORKING pane is not given an older answer. Polling stays at 30 lines.
- Clipboard: one proven SSH client gets OSC 52 and the notice "현재 터미널 클립보드". Any other case uses this computer's clipboard (`clip.exe` on MAINPC) and the notice "이 컴퓨터의 클립보드". If that fails, the tmux buffer is used and the notice does not claim the clipboard succeeded.
- Isolated tmux dogfood PASS: empty tracker, answer above the 30-line window, recovery succeeded. No text was typed into existing agent panes.
- MAINPC local copy PASS: `clip.exe` round trip. It replaced the MAINPC Windows clipboard with a probe string.
- SSH client / ASUS copy FAIL: two tmux clients were attached. Neither process tree showed `sshd` or `SSH_CONNECTION`, so OSC 52 was not sent. ASUS paste was not verified.

## Changed
- Result recovery: `control/actions.py`, Codex/Claude/Cursor/OpenCode extractors, ssh screen adapter, `ui/tower.py`
- Clipboard destination: `clipboard.py`, `ui/control_view.py`, `i18n/ko.py`, `i18n/en.py`
- Commits: `a764010`, `20d427a`

## Verified
- automated: 640 passed, 1 skipped, 0 failed. The skip is the ASUS workspace dogfood (`TOWER_ASUS_TREE` unset).
- local human: isolated recovery dogfood PASS. MAINPC `clip.exe` probe PASS.
- remote/SSH: ASUS clipboard FAIL. Live Y still runs public `main`, not this branch.
- phone: not exercised for this task

## Remaining
- The live MAINPC session still runs public `main`. Y there does not use this branch until that Tower is restarted from `feat/tower-remote`.
- OSC 52 is sent only for exactly one attached client with SSH evidence. Two clients, or no `sshd` / `SSH_CONNECTION` evidence, keep the MAINPC clipboard.
- OSC 52 was confirmed on an isolated pty. It was not confirmed by pasting on the ASUS laptop.
- If a capture has no user prompt and no earlier completion marker, recovery keeps the prose near the completion line, not the whole scrollback.
- A payload over 48KB does not use OSC 52.

## Next PM Decision
- Restart the live MAINPC Tower from `feat/tower-remote`, or leave the public `main` process attached.
- Accept MAINPC clipboard while more than one tmux client is attached, or require a single SSH client before calling ASUS copy done.

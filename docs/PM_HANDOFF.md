# Tmux Agent Tower — PM Handoff

## Current
- Branch: `feat/tower-remote`
- HEAD: branch tip. Recovery and clipboard are `a764010` and `20d427a`.
- Worktree: clean after this handoff commit
- Tests: 640 passed, 1 skipped, 0 failed

## Last Task
- Y said there was no result, then the copy could not reach the laptop clipboard.
- The attached Tower process is still the public `main` checkout. That binary has no result copy. On this branch the second cause is real: the 2s refresh reads about 30 lines, and a finished answer can sit above that.
- Y and S, only when the in-memory tracker is empty, read at most 800 lines and 256KB. They keep the newest finished turn. A WORKING pane is not given an older answer. The refresh stays at 30 lines.
- One proven SSH client gets OSC 52 and the notice "현재 터미널 클립보드". Otherwise the notice is "이 컴퓨터의 클립보드" (`clip.exe` on MAINPC). If that fails, the tmux buffer is used and the notice does not say the clipboard succeeded.
- Isolated tmux dogfood: empty tracker, answer pushed above the 30-line window, recovery succeeded. No text was typed into existing agent panes.
- MAINPC `clip.exe` round trip succeeded. It replaced the MAINPC Windows clipboard with a probe string.
- ASUS paste was not verified. Two tmux clients were attached. Neither process tree showed `sshd` or `SSH_CONNECTION`, so OSC 52 was not sent.

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
- With two attached clients, or without SSH evidence on the client process, OSC 52 is withheld. The copy goes to the MAINPC clipboard.
- OSC 52 was confirmed on an isolated pty. It was not confirmed by pasting on the ASUS laptop.
- If a capture has no user prompt and no earlier completion marker, recovery keeps the prose near the completion line, not the whole scrollback.

## Next PM Decision
- Restart the live MAINPC Tower from `feat/tower-remote`, or leave the public `main` process attached.
- Accept MAINPC clipboard while more than one tmux client is attached, or require a single SSH client before calling ASUS copy done.

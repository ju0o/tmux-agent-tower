# Tmux Agent Tower

**[English](README.md) | 한국어**

tmux pane 여러 개에서 돌아가는 코딩 Agent들이 지금 뭘 하고 있는지 한눈에 봅니다.

Codex, Claude Code, OpenCode, Grok CLI, Cursor Agent CLI를 여러 tmux pane에서
(때로는 여러 대의 컴퓨터에 걸쳐) 동시에 쓰다 보면, 어떤 게 아직 작업 중인지,
어떤 게 승인을 기다리며 멈춰 있는지, 어떤 게 10분 전에 이미 끝났는지 놓치기
쉽습니다. Tmux Agent Tower는 이걸 한 화면에 모아 보여주고, 필요한 pane으로
바로 이동시켜주는 작고 local-first인 TUI입니다.

```
TMUX AGENT TOWER
● 작업 중 3  ! 입력 대기 1  ○ 대기 2  ? 확인 불가 0  × 종료됨 0

▼ MAINPC  ●2 !1 ○1 ?0 ×0
  alpha-api            Codex       ● 작업 중     확인함
> web-app              Codex       ! 입력 대기   새 항목
  docs                 Grok        ○ 대기        확인함

▼ ASUS  ●1 !0 ○1 ?0 ×0
  billing-service      Claude      ● 작업 중     새 항목
  internal-notes       OpenCode    ○ 대기        확인함

↑↓ 이동   Enter 열기   E 이름 변경   N 프로젝트 추가   W 새 작업공간   R 새로고침   Q/Ctrl+C 종료
```

## 이 도구는 이런 것입니다 (그리고 이런 것은 아닙니다)

* **Local-first**: 서버도, 계정도, 클라우드 백엔드도 없습니다. 사용자 본인의
  tmux 서버(그리고 선택적으로, 본인이 이미 맺어둔 SSH 연결을 통한 두 번째
  호스트)를 읽어서 보여줄 뿐입니다.
* **기본적으로 읽기 전용**: 모니터링 중인 pane에 키 입력을 보내지 않고,
  프로세스를 죽이거나 재시작하지 않으며, 실제 Agent CLI나 그 인증정보에는
  손대지 않습니다. tmux 상태를 읽고, Enter를 눌렀을 때 *사용자 본인의* 커서를
  pane 사이로 옮기는 것뿐입니다 -- `Ctrl+b` + 화살표키로 직접 하는 것과 같은 일입니다.
* **최선-추정(best-effort) 상태**이지 보장이 아닙니다. 상태는 각 Agent의
  내부 API가 아니라(대부분 그런 걸 제공하지 않습니다) 터미널 출력 패턴과
  프로세스 활동으로 추정합니다. Agent CLI의 새 버전이 나오면 UI가 바뀌어서
  해당 Agent의 감지가 깨질 수 있습니다 -- [`docs/STATUS_ENGINE.md`](docs/STATUS_ENGINE.md) 참고.

## 기능

* 호스트별로 묶어서 모든 tmux pane의 프로젝트 / 에이전트 / 상태 / 확인여부를
  한 화면에서 봅니다.
* "상태"와 "내가 이걸 봤는지"는 완전히 분리해서 추적합니다 -- 아직 열어보지
  않은 pane에서 Agent가 실제로 작업 중이면 어떤 모호한 "확인 중" 같은 게
  아니라 정확히 `작업 중`으로 표시됩니다.
* 프로젝트 이름은 pane의 git 저장소에서 자동으로 찾습니다(없으면 디렉터리
  이름으로 대체), `E`로 직접 이름을 바꾸면 이후 새로고침에도 유지됩니다.
* SSH로 연결된 두 번째 호스트도 함께 보여주며, 연결이 안 되면 `확인 불가`/오프라인으로
  안전하게 표시됩니다 -- 로컬 TUI는 그것 때문에 멈추지 않습니다.

## 설치

Python 3.9+, tmux, curses를 지원하는 터미널(`TERM`)이 필요합니다(일반적인
터미널이면 다 됩니다).

```bash
git clone https://github.com/ju0o/tmux-agent-tower
cd tmux-agent-tower
./scripts/install.sh
```

설치 스크립트는:

* 사용자 계정에 `tower` 명령을 설치합니다(`pip install --user -e .`, 또는
  `--user` 설치가 막혀 있는 환경(PEP 668)이면 가상환경에 설치),
* `~/.bashrc`에 한 줄만 추가합니다(이미 있으면 건너뜁니다),
* 손대는 dotfile은 먼저 백업합니다(`<파일>.bak.<타임스탬프>`),
* `~/.tmux.conf`는 건드리지 않고, tmux 키도 기본적으로 재바인딩하지
  않습니다 -- 아래 "선택사항: `Ctrl+b w` 단축키" 참고.

설치한 걸 되돌리려면 `./scripts/uninstall.sh`를 실행하세요(뭘 되돌릴지 먼저
알려줍니다).

## 사용법

tmux 세션 안 어디서든:

```
tower
```

이 명령은 `CONTROL` 창을 만들거나 그리로 이동해서 TUI를 띄웁니다 -- **명령을
입력한 그 pane이 아닙니다.** 관제탑은 세션당 하나이고, `tower`는 항상 그
하나의 자리로 데려갑니다. tmux 다른 곳에서 `Ctrl+b` 다음 `w`를 누르는 것과
같은 개념입니다(선택 설정, 아래 참고). 지금 있는 그 pane에서 바로 띄우고
싶다면 `tower --here`를 쓰세요.

| 키 | 동작 |
|---|---|
| `↑` / `↓` (또는 `j`/`k`) | 이동 |
| `Enter` | 선택한 pane으로 이동 |
| `E` | 선택한 pane의 프로젝트 이름을 직접 지정 |
| `N` | 프로젝트 1개 추가 (Workspace Launcher, 단일) |
| `W` | 새 작업공간 시작 (Workspace Launcher, 다중) |
| `R` | 즉시 새로고침 |
| `Q` / `Ctrl+C` | 종료 |

### Workspace Launcher (`N` / `W`)

컴퓨터(호스트) 하나, 프로젝트 하나 이상(설정된 검색 경로에서 자동으로
찾거나, 검색하거나, 직접 경로 입력), 프로젝트별 에이전트를 고른 뒤 미리보기
화면(프로젝트별 에이전트 재지정, 레이아웃 선택 가능)을 보여주고, 확인하면
그 프로젝트들을 위한 **완전히 새로운** tmux pane을 만듭니다 -- 해당
프로젝트로 `cd`하고, 고른 에이전트를 실행하고, 제목을 자동으로 붙이고,
곧바로 Tower에 표시됩니다.

이건 이미 돌아가고 있는 Agent를 조작하는 것과는 분명히 다릅니다: 이
launcher는 항상 새 pane만 만들 뿐, 실행 전부터 있던 어떤 것에도 입력을
보내거나, 닫거나, 재구성하지 않습니다 -- 이 구분이 왜 중요한지, 그리고
아직 의도적으로 만들지 않은 것("Action Layer")이 무엇인지는
[`docs/ROADMAP.md`](docs/ROADMAP.md)를 참고하세요.

프로젝트 검색 위치는 `~/.config/tmux-agent-tower/config.toml`에서 옵니다:

```toml
project_roots = ["~/Projects", "~/code"]

[agents]
claude = "claude-beta"   # 에이전트 실행에 쓸 명령어를 직접 지정
```

둘 다 선택사항입니다 -- 설정이 전혀 없으면 launcher는 홈 폴더 아래에 실제로
있는 흔한 디렉터리 이름들(`Projects`, `code`, `dev`, `src` 등)을 찾고, 각
에이전트는 기본 명령어 이름을 씁니다. 실행 직전에 `command -v`로 명령어가
있는지 확인합니다(로컬 실행이면 로컬에서, 원격 실행이면 대상 호스트에서);
없으면 그 프로젝트의 pane만 "명령을 찾을 수 없음" 제목이 붙은 빈 셸로
열리고, 나머지는 정상적으로 진행됩니다.

### 선택사항: `Ctrl+b w` 단축키

`Ctrl+b w`를 관제탑으로 바로 가는 단축키로 쓰면 편하지만, 이건 tmux
기본 `choose-tree` 창 선택 기능을 그 키에서 **덮어씁니다**. 설치
스크립트는 이걸 자동으로 하지 않습니다. 원하면 `~/.tmux.conf`에 직접
추가하세요:

```tmux
unbind-key w
bind-key w run-shell -b "tower"
```

### `tower --doctor`

환경을 빠르게 점검합니다(tmux/Python/curses 사용 가능 여부, tmux 안에서
실행 중인지, 설정된 원격 호스트 등)와 항목별 결과를 출력합니다.

### 두 번째 호스트

`~/.config/tmux-agent-tower/remote-hosts.txt`에, 이미 `~/.ssh/config`에
설정해둔 SSH alias를 한 줄 추가하세요:

```
asus:ASUS
```

그러면 Tower가 그 호스트의 pane들도 함께 보여줍니다(제목/명령어 기반
상태만 -- 아래 제한사항 참고), 로컬 호스트보다 새로고침 주기는 더 길고,
연결이 안 되면 `확인 불가`/오프라인으로 안전하게 표시됩니다. 원격
호스트에는 아무것도 설치되지 않습니다. Workspace Launcher(`N`/`W`)도 이
호스트를 대상으로 쓸 수 있습니다: SSH로 읽기 전용 `find`를 한 번 실행해서
그쪽의 git 저장소도 찾아주고, 아무것도 못 찾으면 마찬가지로 직접 경로
입력(`B`)으로 넘어갑니다.

### 언어

TUI는 맨 처음 실행할 때 딱 한 번 한국어/영어를 물어보고, 그 답을
`~/.config/tmux-agent-tower/config.toml`에 저장합니다(`language = "ko"` /
`"en"`). 이번 릴리스에는 앱 안에서 다시 바꾸는 기능은 없습니다 -- 바꾸고
싶으면 그 줄을 직접 수정하고 `tower`를 다시 실행하세요.

## 지원 Agent

Codex, Claude Code, OpenCode, Grok CLI, Cursor Agent CLI, 그리고 그 외
전부를 위한 일반 shell/SSH 폴백. 모든 어댑터의 작업중/대기 판정(그리고
OpenCode를 제외한 입력대기 판정)은 실제로 그 CLI를 띄워 관찰한 뒤
만들어졌습니다 -- 각 어댑터가 정확히 어떤 근거를 보는지는
[`docs/STATUS_ENGINE.md`](docs/STATUS_ENGINE.md)를 참고하세요. OpenCode의
자체 승인/확인 프롬프트만은 실제로 관찰하지 못했습니다(테스트한 세션이
쓰기 작업을 자동 승인하는 설정이었습니다), 그래서 그 Agent의 입력대기
판정만 일반 패턴에 의존합니다 -- `adapters/opencode.py`의 설명 참고.
감지는 본질적으로 최선-추정이며 이 CLI들의 UI가 바뀌면 함께 낡아질 수
있습니다; 검증된(sanitize된) fixture와 함께 패턴을 고치는 PR을 환영합니다.

## 상태 의미

* `● 작업 중` -- 실제로 출력이 생기고 있거나 Agent 고유의 "작업 중" 신호가
  있습니다.
* `! 입력 대기` -- 사용자의 승인/입력을 기다리는 것으로 보입니다.
* `○ 대기` -- 살아있지만 대기 중인 건 없습니다.
* `? 확인 불가` -- 판단할 근거가 충분하지 않습니다. 이건 의도적으로 정직한
  fallback입니다 -- 왜 "모름"이 자신만만한 오답보다 나은지는
  `docs/STATUS_ENGINE.md` 참고.
* `× 종료됨` -- pane 또는 프로세스가 종료되었습니다.

`확인`(`새 항목`/`확인함`)은 상태와 완전히 독립적입니다 -- Tower에서 그
pane을 한 번이라도 열어봤는지만 추적합니다.

## 제한사항

* 상태 감지는 각 CLI의 *현재* 터미널 UI를 기준으로 한 패턴 매칭입니다.
  그 도구들이 바뀌면 낡아질 수 있습니다; 검증된 fixture와 함께 패턴을
  고치는 PR을 환영합니다.
* 원격/다중 호스트 화면은 pane 제목만 보고 실제 내용은 보지 않습니다(원격에
  아무것도 설치하지 않기 위한 의도적인 단순화 -- `docs/ARCHITECTURE.md`
  참고), 그래서 원격 상태는 로컬보다 거칠게 판단됩니다.
* 직접 지정한 이름(`E`)은 tmux의 pane id를 키로 삼는데, 이 id는 tmux 서버가
  재시작되면 재사용됩니다; 드물게 이전 이름이 엉뚱한 나중 pane에 "들러붙을"
  수 있습니다.
* Linux/WSL + tmux 환경에서 테스트했습니다. macOS도 (같은 Python + tmux +
  curses 조합이라) 대체로 동작할 것으로 보이지만 여기서 직접 검증하지는
  않았습니다.

## 제거

```bash
./scripts/uninstall.sh
```

## 로드맵

[`docs/ROADMAP.md`](docs/ROADMAP.md) 참고. Agent에 입력을 보내거나,
중단/재시작시키거나, 승인 프롬프트를 자동으로 처리하는 기능은 분명히
**구현하지 않았고**, 별도의 신중한 설계 검토 없이는 계획에도 없습니다 --
이 도구는 항상 읽고 이동만 시켜줍니다.

## 라이선스

MIT -- [`LICENSE`](LICENSE) 참고.

"""The one static page Tower Remote serves.

Two views, no build step, no dependency, no external CDN:

* list -- host-grouped project cards (polled every 2.5 s);
* detail -- one pane: identity, status, a LIVE PANE box fed by
  ``GET /api/panes/<key>/screen`` (recent plain text, polled about once a
  second and backing off when the round trip is slow), an edit form that
  writes the same overrides the PC's ``E`` menu does, and the explicit
  prompt textarea + send button.

Deliberately not a terminal emulator: no scrollback streaming, no
WebSocket, no colour. Polling stops entirely while the page is hidden.
"""

from __future__ import annotations

# Client-side polling policy (mirrored in tests by name).
LIST_POLL_MS = 2500
SCREEN_POLL_MIN_MS = 1000
SCREEN_POLL_MAX_MS = 2000
SCREEN_POLL_SLOW_MS = 700  # a round trip slower than this backs polling off

PAGE_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Tmux Agent Tower</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0f1115; --card: #1a1d24; --line: #2a2f3a;
    --fg: #e7e9ee; --dim: #8b92a3;
    --working: #4fb0ff; --waiting: #ffb84f; --idle: #6b7280;
    --unknown: #c084fc; --dead: #ef4444;
  }
  * { box-sizing: border-box; }
  html, body { max-width: 100%; overflow-x: hidden; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    padding: 12px 12px 32px;
  }
  button { font: inherit; }
  #banner {
    display: none; background: var(--dead); color: #fff; font-size: 13px;
    padding: 8px 12px; border-radius: 8px; margin-bottom: 10px; text-align: center;
  }
  #banner.show { display: block; }
  #source-gone {
    display: none; background: var(--card); border: 1px solid var(--waiting); border-radius: 10px;
    padding: 16px 14px; margin-bottom: 10px; line-height: 1.5;
  }
  #source-gone.show { display: block; }
  #source-gone .title { font-weight: 600; margin-bottom: 4px; }
  #source-gone .sub { font-size: 13px; color: var(--dim); }
  h1 { font-size: 15px; letter-spacing: .04em; color: var(--dim); margin: 4px 4px 12px; text-transform: uppercase; }
  .host { font-size: 12px; color: var(--dim); margin: 16px 4px 6px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 12px; margin-bottom: 8px; display: flex; flex-direction: column; gap: 3px;
    cursor: pointer;
  }
  .card.attention { border-color: var(--waiting); background: #241d12; }
  .row1 { display: flex; align-items: center; gap: 8px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .dot.WORKING { background: var(--working); }
  .dot.WAITING { background: var(--waiting); }
  .dot.IDLE { background: var(--idle); }
  .dot.UNKNOWN { background: var(--unknown); }
  .dot.DEAD { background: var(--dead); }
  .project { font-weight: 600; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .agent { font-size: 12px; color: var(--dim); }
  .status-line { font-size: 13px; color: var(--dim); }
  .activity { font-size: 12px; color: var(--dim); font-style: italic; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .offline { font-size: 13px; color: var(--dim); padding: 4px; }

  /* -- detail view ------------------------------------------------- */
  #detail { display: none; }
  #detail.open { display: block; }
  #main.hidden { display: none; }
  .d-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .d-head button {
    min-width: 44px; min-height: 44px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--card); color: var(--fg); font-size: 18px;
  }
  .d-title { font-weight: 700; font-size: 17px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .d-meta { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; }
  .d-meta .kv { display: flex; gap: 10px; font-size: 13px; padding: 2px 0; }
  .d-meta .k { color: var(--dim); min-width: 74px; flex: none; }
  .d-meta .v { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .d-meta .v.wrap { white-space: normal; }
  .live-label { font-size: 11px; letter-spacing: .08em; color: var(--dim); margin: 10px 4px 4px; display: flex; justify-content: space-between; }
  #screen-wrap {
    background: #0a0c10; border: 1px solid var(--line); border-radius: 10px;
    overflow-x: auto; overflow-y: auto; max-height: 55vh; min-height: 160px;
    -webkit-overflow-scrolling: touch;
  }
  #screen {
    margin: 0; padding: 10px 12px; font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    white-space: pre; color: #d9dde6; min-width: 100%; width: max-content;
  }
  #screen.stale { opacity: .55; }
  #result-box { display: none; margin: 8px 0 12px; }
  #result-box.open { display: block; }
  #result-text { white-space: pre-wrap; word-break: break-word; user-select: text; -webkit-user-select: text; background: #12141a; border: 1px solid #2a3142; border-radius: 8px; padding: 10px; min-height: 0; max-height: 28vh; overflow: auto; }
  .result-flag { color: #8fd18f; font-weight: 650; margin-top: 4px; }
  .result-flag.read { color: #8b93a7; font-weight: 500; }
  .card.waiting { border-color: #c9a227; }
  .send-box { margin-top: 10px; }
  .send-box .target { font-size: 12px; color: var(--dim); margin-bottom: 6px; }
  .send-box .target b { color: var(--fg); }
  textarea {
    width: 100%; min-height: 76px; background: #11141a; color: var(--fg);
    border: 1px solid var(--line); border-radius: 8px; padding: 10px; font-size: 16px; line-height: 1.4;
    font-family: inherit; resize: vertical;
  }
  .actions { display: flex; gap: 8px; margin-top: 8px; }
  .actions button {
    flex: 1; min-height: 46px; padding: 10px; border-radius: 8px; border: none; font-size: 16px; font-weight: 600;
  }
  #send { background: var(--working); color: #041018; }
  #send:disabled { opacity: .6; }
  #edit-toggle, #edit-cancel { background: var(--line); color: var(--fg); }
  #edit-save { background: var(--working); color: #041018; }
  #edit-reset { background: transparent; color: var(--waiting); border: 1px solid var(--waiting) !important; }
  #edit { display: none; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; margin-top: 10px; }
  #edit.open { display: block; }
  #edit label { display: block; font-size: 12px; color: var(--dim); margin: 8px 0 4px; }
  #edit input {
    width: 100%; min-height: 44px; font-size: 16px; padding: 8px 10px; border-radius: 8px;
    border: 1px solid var(--line); background: #11141a; color: var(--fg);
  }
  #edit .hint { font-size: 12px; color: var(--dim); margin-top: 2px; }

  #pair-screen { display: flex; flex-direction: column; gap: 10px; max-width: 320px; margin: 40px auto; }
  #pair-screen input {
    font-size: 20px; padding: 12px; min-height: 48px; border-radius: 8px; border: 1px solid var(--line);
    background: #11141a; color: var(--fg); text-align: center; letter-spacing: .2em;
  }
  #pair-screen button {
    min-height: 48px; padding: 12px; border-radius: 8px; border: none;
    background: var(--working); color: #041018; font-size: 16px; font-weight: 600;
  }
  #pair-error { color: var(--dead); font-size: 13px; min-height: 1.2em; }
  #toast {
    position: fixed; top: 10px; left: 50%; transform: translateX(-50%);
    background: var(--line); color: var(--fg); padding: 8px 14px; border-radius: 8px;
    font-size: 13px; display: none; z-index: 5; max-width: 90vw;
  }
</style>
</head>
<body>
<div id="toast"></div>

<div id="pair-screen">
  <h1 style="margin:0 0 6px">Tmux Agent Tower</h1>
  <p style="color:var(--dim); font-size:13px; margin:0">PC의 Tower 화면에 표시된 페어링 코드를 입력하세요.</p>
  <input id="pair-code" inputmode="numeric" maxlength="6" placeholder="000000">
  <button id="pair-submit">연결</button>
  <div id="pair-error"></div>
</div>

<div id="main" style="display:none">
  <h1>Tmux Agent Tower</h1>
  <div id="banner"></div>
  <div id="source-gone">
    <div class="title">관제 중이던 tmux 세션이 종료되었습니다.</div>
    <div class="sub">The tmux session this remote was watching no longer exists.
    On the PC, open <b>tower</b> and press <b>M</b> → start the phone remote again.</div>
    <div class="sub" id="source-gone-name"></div>
  </div>
  <div id="list"></div>
</div>

<div id="detail">
  <div class="d-head">
    <button id="back" aria-label="뒤로">←</button>
    <div class="d-title" id="d-project"></div>
  </div>
  <div class="d-meta">
    <div class="kv"><span class="k">Agent</span><span class="v" id="d-agent"></span></div>
    <div class="kv"><span class="k">상태</span><span class="v" id="d-status"></span></div>
    <div class="kv"><span class="k">현재 작업</span><span class="v wrap" id="d-activity"></span></div>
    <div class="kv"><span class="k">Pane 이름</span><span class="v" id="d-title"></span></div>
    <div class="kv"><span class="k">Host</span><span class="v" id="d-host"></span></div>
    <div class="kv" id="result-row"><span class="k">결과</span><span class="v" id="d-result">-</span></div>
  </div>

  <div id="result-box">
    <pre id="result-text"></pre>
    <div class="actions">
      <button id="result-view" type="button">결과 보기</button>
      <button id="result-copy" type="button">결과 복사</button>
    </div>
    <div class="hint" id="result-msg"></div>
  </div>

  <div class="live-label"><span>LIVE PANE</span><span id="live-meta"></span></div>
  <div id="screen-wrap"><pre id="screen"></pre></div>

  <div class="send-box">
    <div class="target">전송 대상: <b id="send-target"></b></div>
    <textarea id="prompt-text" placeholder="이 pane에 보낼 프롬프트를 입력하세요..." maxlength="4000"></textarea>
    <div class="actions">
      <button id="send">전송</button>
      <button id="edit-toggle">편집</button>
    </div>
  </div>

  <div id="edit">
    <label for="edit-project">프로젝트 이름</label>
    <input id="edit-project" maxlength="80">
    <div class="hint" id="edit-project-auto"></div>
    <label for="edit-agent">Agent 이름</label>
    <input id="edit-agent" maxlength="80">
    <div class="hint" id="edit-agent-auto"></div>
    <label for="edit-title">Pane 이름</label>
    <input id="edit-title" maxlength="80">
    <div class="actions">
      <button id="edit-save">저장</button>
      <button id="edit-cancel">취소</button>
    </div>
    <div class="actions">
      <button id="edit-reset">자동 감지로 복원</button>
    </div>
  </div>
</div>

<script>
(function () {
  "use strict";

  var LIST_POLL_MS = __LIST_POLL_MS__;
  var SCREEN_POLL_MIN_MS = __SCREEN_POLL_MIN_MS__;
  var SCREEN_POLL_MAX_MS = __SCREEN_POLL_MAX_MS__;
  var SCREEN_POLL_SLOW_MS = __SCREEN_POLL_SLOW_MS__;

  var TOKEN_KEY = "tower_remote_token";
  var token = null;
  try { token = localStorage.getItem(TOKEN_KEY); } catch (e) {}

  var $ = function (id) { return document.getElementById(id); };
  var pairScreen = $("pair-screen"), main = $("main"), list = $("list"), banner = $("banner");
  var sourceGone = $("source-gone"), sourceGoneName = $("source-gone-name");
  var detail = $("detail"), screenEl = $("screen"), screenWrap = $("screen-wrap"), liveMeta = $("live-meta");
  var promptText = $("prompt-text"), sendBtn = $("send"), toast = $("toast");
  var resultBox = $("result-box"), resultText = $("result-text"), resultMsg = $("result-msg");
  var resultFingerprint = "";
  var editBox = $("edit");

  var panes = {};        // key -> last status row
  var current = null;    // key of the open detail, or null
  var sending = false;
  var screenTimer = null, listTimer = null;
  var screenInterval = SCREEN_POLL_MIN_MS;
  var screenInFlight = false;
  var lastScreenText = null;

  function showToast(msg) {
    toast.textContent = msg;
    toast.style.display = "block";
    setTimeout(function () { toast.style.display = "none"; }, 2500);
  }

  function setBanner(msg) {
    if (!msg) { banner.classList.remove("show"); return; }
    banner.textContent = msg;
    banner.classList.add("show");
  }

  function showSourceGone(session) {
    list.innerHTML = "";
    closeDetail();
    sourceGoneName.textContent = session ? "session: " + session : "";
    sourceGone.classList.add("show");
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (token) opts.headers["Authorization"] = "Bearer " + token;
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (body) { return { status: r.status, body: body }; });
    });
  }

  function paneUrl(key, action) {
    return "/api/panes/" + encodeURIComponent(key) + "/" + action;
  }

  $("pair-submit").addEventListener("click", function () {
    var code = $("pair-code").value.trim();
    var err = $("pair-error");
    err.textContent = "";
    fetch("/api/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code }),
    }).then(function (r) { return r.json().then(function (b) { return { status: r.status, body: b }; }); })
      .then(function (res) {
        if (res.status === 200 && res.body.ok) {
          token = res.body.token;
          try { localStorage.setItem(TOKEN_KEY, token); } catch (e) {}
          pairScreen.style.display = "none";
          main.style.display = "block";
          pollList();
        } else {
          err.textContent = "코드가 틀렸거나 만료되었습니다.";
        }
      })
      .catch(function () { err.textContent = "Tower에 연결할 수 없습니다."; });
  });

  function statusLabel(s) {
    return { WORKING: "작업 중", WAITING: "입력 대기", IDLE: "대기", UNKNOWN: "확인 불가", DEAD: "종료" }[s] || s;
  }

  function statusDot(s) {
    return { WORKING: "●", WAITING: "!", IDLE: "○", UNKNOWN: "?", DEAD: "✕" }[s] || "○";
  }

  function formatDuration(sec) {
    sec = Math.max(0, sec | 0);
    if (sec < 60) return sec + "s";
    var m = (sec / 60) | 0;
    if (m < 60) return m + "m";
    var h = (m / 60) | 0;
    return h + "h";
  }

  // -- list view ----------------------------------------------------

  function render(payload) {
    list.innerHTML = "";
    panes = {};
    var byHost = {};
    var order = [];
    payload.panes.forEach(function (p) {
      panes[p.key] = p;
      if (!byHost[p.host]) { byHost[p.host] = []; order.push(p.host); }
      byHost[p.host].push(p);
    });

    order.forEach(function (host) {
      var h = document.createElement("div");
      h.className = "host";
      h.textContent = host;
      list.appendChild(h);

      byHost[host].sort(function (a, b) {
        function rank(p) {
          if (p.status === "WAITING") return 0;
          if (p.result_state === "ready") return 1;
          if (p.status === "WORKING") return 2;
          if (p.status === "IDLE") return 3;
          return 4;
        }
        return rank(a) - rank(b);
      });
      byHost[host].forEach(function (p) {
        if (p.offline) {
          var off = document.createElement("div");
          off.className = "offline";
          off.textContent = "(연결 안 됨 / tmux 서버 없음)";
          list.appendChild(off);
          return;
        }

        var card = document.createElement("div");
        card.className = "card" + (p.status === "WAITING" ? " attention waiting" : "");
        card.addEventListener("click", function () { openDetail(p.key); });

        var row1 = document.createElement("div");
        row1.className = "row1";
        var dot = document.createElement("div");
        dot.className = "dot " + p.status;
        var proj = document.createElement("div");
        proj.className = "project";
        proj.textContent = p.project || "(이름 없음)";
        var agent = document.createElement("div");
        agent.className = "agent";
        agent.textContent = p.agent || "";
        row1.appendChild(dot); row1.appendChild(proj); row1.appendChild(agent);

        var statusLine = document.createElement("div");
        statusLine.className = "status-line";
        statusLine.textContent = statusLabel(p.status) + " · " + formatDuration(p.duration_seconds);

        card.appendChild(row1);
        card.appendChild(statusLine);

        if (p.activity) {
          var act = document.createElement("div");
          act.className = "activity";
          act.textContent = p.activity;
          card.appendChild(act);
        }
        if (p.result_state === "ready") {
          var flag = document.createElement("div");
          flag.className = "result-flag";
          flag.textContent = "✓ 새 Result";
          card.appendChild(flag);
        }
        list.appendChild(card);
      });
    });

    if (current) fillDetailMeta(panes[current]);
  }

  function pollList() {
    if (document.hidden) return;
    api("/api/status").then(function (res) {
      if (res.status === 401) {
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
        token = null;
        closeDetail();
        main.style.display = "none";
        pairScreen.style.display = "flex";
        return;
      }
      setBanner(null);
      if (res.body && res.body.error === "source_session_missing") {
        showSourceGone(res.body.session);
        return;
      }
      if (res.status !== 200 || !res.body || !res.body.panes) {
        setBanner("Tower 오류 (" + ((res.body && res.body.error) || res.status) + ")");
        return;
      }
      sourceGone.classList.remove("show");
      render(res.body);
    }).catch(function () {
      setBanner("Tower에 연결할 수 없습니다 — 아래 내용은 오래된 것일 수 있습니다.");
    });
  }

  // -- detail view --------------------------------------------------

  function fillDetailMeta(p) {
    if (!p) return;
    $("d-project").textContent = p.project || "(이름 없음)";
    $("d-agent").textContent = p.agent || "-";
    $("d-status").textContent = statusDot(p.status) + " " + statusLabel(p.status) + " · " + formatDuration(p.duration_seconds);
    $("d-activity").textContent = p.activity || "-";
    $("d-title").textContent = p.pane_title || "-";
    $("d-host").textContent = p.host || "-";
    var resultLabel = $("d-result");
    if (p.result_state === "ready") resultLabel.textContent = "✓ 새 Result";
    else if (p.result_state === "read") resultLabel.textContent = "확인한 Result";
    else resultLabel.textContent = "-";
    resultBox.classList.toggle("open", p.result_state === "ready" || p.result_state === "read");
    // The target is always visible right above the textarea.
    $("send-target").textContent = (p.project || "(이름 없음)") + " / " + (p.agent || "-");
  }

  function openDetail(key) {
    var p = panes[key];
    if (!p) return;
    current = key;
    fillDetailMeta(p);
    screenEl.textContent = "";
    screenEl.classList.remove("stale");
    lastScreenText = null;
    liveMeta.textContent = "";
    promptText.value = "";
    resultText.textContent = "";
    resultMsg.textContent = "";
    resultFingerprint = "";
    editBox.classList.remove("open");
    main.classList.add("hidden");
    detail.classList.add("open");
    if (p.remote) {
      screenEl.textContent = "(원격 SSH 호스트의 pane은 제목/상태만 표시됩니다. 실시간 화면은 이 PC의 pane에서만 볼 수 있습니다.)";
      sendBtn.disabled = true;
      return;
    }
    sendBtn.disabled = false;
    screenInterval = SCREEN_POLL_MIN_MS;
    pollScreen();
  }

  function closeDetail() {
    current = null;
    detail.classList.remove("open");
    main.classList.remove("hidden");
    if (screenTimer) { clearTimeout(screenTimer); screenTimer = null; }
  }

  $("back").addEventListener("click", closeDetail);

  function scheduleScreen() {
    if (screenTimer) clearTimeout(screenTimer);
    if (!current || document.hidden) return;
    screenTimer = setTimeout(pollScreen, screenInterval);
  }

  function pollScreen() {
    if (!current || document.hidden || screenInFlight) return;
    var key = current;
    var p = panes[key];
    if (p && p.remote) return;
    screenInFlight = true;
    var started = Date.now();
    api(paneUrl(key, "screen")).then(function (res) {
      screenInFlight = false;
      if (current !== key) return;
      var rtt = Date.now() - started;
      // Adaptive: slow round trips (LTE, busy PC) back off toward the max.
      screenInterval = rtt > SCREEN_POLL_SLOW_MS
        ? Math.min(SCREEN_POLL_MAX_MS, screenInterval + 250)
        : Math.max(SCREEN_POLL_MIN_MS, screenInterval - 250);
      if (res.status === 200 && res.body && res.body.lines) {
        var text = res.body.lines.join("\\n");
        if (text !== lastScreenText) {
          var atBottom = screenWrap.scrollHeight - screenWrap.scrollTop - screenWrap.clientHeight < 24;
          screenEl.textContent = text || "(비어 있음)";
          lastScreenText = text;
          if (atBottom) screenWrap.scrollTop = screenWrap.scrollHeight;
        }
        screenEl.classList.remove("stale");
        liveMeta.textContent = rtt + "ms · " + (screenInterval / 1000) + "s" + (res.body.truncated ? " · 일부" : "");
      } else if (res.body && res.body.error === "source_session_missing") {
        closeDetail();
        showSourceGone(res.body.session);
        return;
      } else {
        screenEl.classList.add("stale");
        liveMeta.textContent = (res.body && res.body.error) || ("HTTP " + res.status);
        if (res.body && (res.body.error === "not_found" || res.body.error === "stale")) {
          showToast("이 pane은 더 이상 존재하지 않습니다.");
          closeDetail();
          return;
        }
      }
      scheduleScreen();
    }).catch(function () {
      screenInFlight = false;
      if (current !== key) return;
      screenEl.classList.add("stale");
      liveMeta.textContent = "연결 끊김";
      screenInterval = SCREEN_POLL_MAX_MS;
      scheduleScreen();
    });
  }

  // -- prompt send: only from the detail view, after seeing the pane --

  function markResultRead() {
    if (!current || !resultFingerprint) return;
    api(paneUrl(current, "result"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fingerprint: resultFingerprint })
    }).then(function () { pollList(); });
  }

  function loadResult() {
    if (!current) return Promise.resolve(null);
    return api(paneUrl(current, "result")).then(function (res) {
      if (!res.body || !res.body.ok) {
        resultMsg.textContent = "Result를 가져오지 못했습니다.";
        return null;
      }
      resultFingerprint = res.body.fingerprint || "";
      resultText.textContent = res.body.text || "";
      return res.body.text || "";
    });
  }

  $("result-view").addEventListener("click", function () {
    loadResult().then(function (text) {
      if (text) {
        resultMsg.textContent = "";
        markResultRead();
      }
    });
  });

  $("result-copy").addEventListener("click", function () {
    loadResult().then(function (text) {
      if (!text) {
        resultMsg.textContent = "복사할 Result가 없습니다.";
        return;
      }
      var done = function () {
        resultMsg.textContent = "Result를 복사했습니다.";
        markResultRead();
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(function () {
          resultMsg.textContent = "자동 복사에 실패했습니다. 아래 글을 길게 눌러 선택하세요.";
        });
      } else {
        resultMsg.textContent = "자동 복사에 실패했습니다. 아래 글을 길게 눌러 선택하세요.";
      }
    });
  });

  sendBtn.addEventListener("click", function () {
    if (!current || sending) return;
    var p = panes[current];
    if (!p) return;
    var text = promptText.value;
    if (!text.trim()) return;
    if (p.status === "DEAD") { showToast("이 pane은 종료되었습니다."); return; }
    if (p.agent === "Shell" || p.agent === "SSH") {
      if (!window.confirm("이 pane은 일반 셸입니다. 입력한 내용이 그대로 실행됩니다. 보낼까요?")) return;
    }

    sending = true;
    sendBtn.disabled = true;
    api("/api/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pane_key: p.key, text: text, project: p.project, agent: p.agent }),
    }).then(function (res) {
      sending = false;
      sendBtn.disabled = false;
      if (res.status === 200 && res.body.ok) {
        showToast("전송했습니다.");
        promptText.value = "";
        pollScreen();
      } else {
        showToast("전송 실패 (" + (res.body.error || res.status) + ")");
      }
    }).catch(function () {
      sending = false;
      sendBtn.disabled = false;
      showToast("Tower에 연결할 수 없습니다.");
    });
  });

  // -- identity edit: same OverrideStore as the PC's E menu -----------

  $("edit-toggle").addEventListener("click", function () {
    var p = panes[current];
    if (!p) return;
    $("edit-project").value = p.project || "";
    $("edit-agent").value = p.agent || "";
    $("edit-title").value = p.pane_title || "";
    $("edit-project-auto").textContent = p.auto_project ? "자동: " + p.auto_project : "";
    $("edit-agent-auto").textContent = p.auto_agent ? "자동: " + p.auto_agent : "";
    editBox.classList.toggle("open");
  });

  $("edit-cancel").addEventListener("click", function () { editBox.classList.remove("open"); });

  function postIdentity(body, done) {
    var key = current;
    if (!key) return;
    api(paneUrl(key, "identity"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (res) {
      if (res.status === 200 && res.body.ok) {
        showToast(done);
        editBox.classList.remove("open");
        pollList();
      } else {
        showToast("저장 실패 (" + (res.body.error || res.status) + ")");
      }
    }).catch(function () { showToast("Tower에 연결할 수 없습니다."); });
  }

  $("edit-save").addEventListener("click", function () {
    var p = panes[current];
    if (!p) return;
    var body = {};
    var project = $("edit-project").value.trim();
    var agent = $("edit-agent").value.trim();
    var title = $("edit-title").value.trim();
    if (project !== (p.project || "")) body.project = project || null;
    if (agent !== (p.agent || "")) body.agent = agent || null;
    if (title && title !== (p.pane_title || "")) body.title = title;
    if (Object.keys(body).length === 0) { editBox.classList.remove("open"); return; }
    postIdentity(body, "저장했습니다. PC Tower에도 반영됩니다.");
  });

  $("edit-reset").addEventListener("click", function () {
    postIdentity({ reset: true }, "자동 감지로 복원했습니다.");
  });

  // -- polling lifecycle: nothing runs while the page is hidden -------

  function startTimers() {
    if (listTimer) clearInterval(listTimer);
    listTimer = setInterval(pollList, LIST_POLL_MS);
    pollList();
    if (current) pollScreen();
  }

  function stopTimers() {
    if (listTimer) { clearInterval(listTimer); listTimer = null; }
    if (screenTimer) { clearTimeout(screenTimer); screenTimer = null; }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stopTimers(); } else { startTimers(); }
  });

  if (token) {
    pairScreen.style.display = "none";
    main.style.display = "block";
  }
  startTimers();
})();
</script>
</body>
</html>
""".replace("__LIST_POLL_MS__", str(LIST_POLL_MS)).replace(
    "__SCREEN_POLL_MIN_MS__", str(SCREEN_POLL_MIN_MS)
).replace("__SCREEN_POLL_MAX_MS__", str(SCREEN_POLL_MAX_MS)).replace(
    "__SCREEN_POLL_SLOW_MS__", str(SCREEN_POLL_SLOW_MS)
)

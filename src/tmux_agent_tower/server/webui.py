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

from ..control.actions import MAX_PROMPT_CHARS

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
<title>에이전트 관제탑</title>
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
  #counts { font-size: 14px; font-weight: 650; margin: 0 4px 8px; letter-spacing: .01em; }
  #counts:empty { display: none; }
  #d-badges { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; }
  #d-badges span { background: var(--card); border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; font-size: 13px; }
  .live-tools { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 6px; }
  .live-tools button { flex: 0 0 auto; min-height: 36px; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--card); color: var(--fg); font-size: 13px; }
  #screen-wrap.full { position: fixed; inset: 0; z-index: 6; max-height: none; min-height: 100vh; border-radius: 0; }
  .live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--working); margin-right: 6px; }
  .live-dot.paused { background: var(--idle); }
  .page-intro, .detail-intro { color: var(--dim); margin: -6px 4px 10px; }
  .empty-state { background: var(--card); border: 1px solid var(--line); border-radius: 10px; color: var(--dim); padding: 14px; line-height: 1.5; }
  details.advanced { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 0 12px; margin-bottom: 8px; }
  details.advanced summary { min-height: 44px; display: flex; align-items: center; cursor: pointer; color: var(--dim); }
  details.advanced[open] { padding-bottom: 10px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 12px; margin-bottom: 8px; display: flex; flex-direction: column; gap: 3px;
    cursor: pointer;
  }
  .work-group { border: 1px solid var(--line); border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
  .work-folder, .work-window { border: 1px solid var(--line); border-radius: 9px; margin: 6px 0; overflow: hidden; }
  .work-folder > summary, .work-window > summary { list-style: none; cursor: pointer; padding: 10px 12px; background: #20242d; }
  .work-window > summary { background: #191c23; }
  .work-folder > summary::-webkit-details-marker, .work-window > summary::-webkit-details-marker { display: none; }
  .work-folder > summary::before, .work-window > summary::before { content: "▶"; display: inline-block; margin-right: 8px; color: var(--dim); }
  .work-folder[open] > summary::before, .work-window[open] > summary::before { content: "▼"; }
  .folder-name, .window-name { font-weight: 650; }
  .folder-summary, .window-summary { display: block; margin: 3px 0 0 20px; color: var(--dim); font-size: 13px; }
  .window-content { padding: 2px 7px 6px 14px; }
  .work-group > summary { list-style: none; cursor: pointer; padding: 12px; background: #20242d; }
  .work-group > summary::-webkit-details-marker { display: none; }
  .work-group > summary::before { content: "▶"; display: inline-block; margin-right: 8px; color: var(--dim); }
  .work-group[open] > summary::before { content: "▼"; }
  .work-group-name { font-weight: 650; }
  .work-group-meta, .work-group-summary { display: block; margin: 3px 0 0 20px; color: var(--dim); font-size: 13px; }
  .group-tools { display: flex; gap: 6px; padding: 6px 10px; }
  .group-tools select { flex: 1; min-width: 0; min-height: 42px; padding: 6px; border-radius: 8px; border: 1px solid var(--line); background: #11141a; color: var(--fg); }
  .group-tools button { min-width: 44px; min-height: 42px; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--card); color: var(--fg); }
  .work-group .card { margin: 6px 8px; }
  .work-group .offline { margin: 6px 8px; }
  .worksets { margin: 14px 0; }
  .worksets h2 { font-size: 16px; margin: 12px 2px 6px; }
  .workset-card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; margin-bottom: 8px; padding: 10px; }
  .workset-card summary { cursor: pointer; font-weight: 650; }
  .workset-meta, .workset-member { color: var(--dim); font-size: 13px; margin: 5px 0; }
  .workset-card label { display: block; color: var(--dim); font-size: 12px; margin: 8px 0 4px; }
  .workset-card input, .workset-card select { width: 100%; min-height: 42px; padding: 8px; border-radius: 8px; border: 1px solid var(--line); background: #11141a; color: var(--fg); font-size: 15px; }
  .workset-card button { width: 100%; min-height: 44px; margin-top: 10px; border: 0; border-radius: 8px; background: var(--working); color: #041018; font-weight: 650; }
  .card.attention { border-color: var(--waiting); background: #241d12; }
  .row1 { display: flex; align-items: center; gap: 8px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .dot.WORKING { background: var(--working); }
  .dot.WAITING { background: var(--waiting); }
  .dot.IDLE { background: var(--idle); }
  .dot.UNKNOWN { background: var(--unknown); }
  .dot.DEAD { background: var(--dead); }
  .project { font-weight: 600; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .project-meta { color: var(--dim); font-size: 12px; margin: 3px 0 0 17px; }
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
  #edit-toggle, #edit-cancel, #focus-pc { background: var(--line); color: var(--fg); }
  #focus-pc { width: 100%; margin-top: 8px; }
  #edit-save { background: var(--working); color: #041018; }
  #edit-reset { background: transparent; color: var(--waiting); border: 1px solid var(--waiting) !important; }
  #edit { display: none; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px; margin-top: 10px; }
  #edit.open { display: block; }
  #edit label { display: block; font-size: 12px; color: var(--dim); margin: 8px 0 4px; }
  #edit input, #edit select {
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
  <h1 style="margin:0 0 6px">에이전트 관제탑</h1>
  <p style="color:var(--dim); font-size:13px; margin:0">컴퓨터의 관제탑 화면에 표시된 연결 코드를 입력하세요.</p>
  <input id="pair-code" inputmode="numeric" maxlength="6" placeholder="000000">
  <button id="pair-submit">연결</button>
  <div id="pair-error"></div>
</div>

<div id="main" style="display:none">
  <h1>에이전트 관제탑</h1>
  <p class="page-intro">작업 묶음과 Agent 상태를 보고 필요한 작업을 눌러 보세요.</p>
  <div id="counts"></div>
  <section class="worksets"><h2>저장된 작업</h2><p>하던 작업</p><div id="saved-worksets"></div><p>작업 템플릿</p><div id="work-templates"></div></section>
  <div id="banner"></div>
  <div id="source-gone">
    <div class="title">연결하던 작업 화면이 끝났어요.</div>
    <div class="sub">컴퓨터에서 관제탑을 열고 휴대폰 연결을 다시 시작해 주세요.</div>
  </div>
  <div id="list"></div>
</div>

<div id="detail">
  <div class="d-head">
    <button id="back" aria-label="뒤로">←</button>
    <div class="d-title" id="d-project"></div>
  </div>
  <p class="detail-intro">상태를 확인하고 필요한 작업을 선택하세요.</p>
  <div id="d-badges"></div>
  <div class="d-meta">
    <div class="kv" id="d-group-row" style="display:none"><span class="k">작업 묶음</span><span class="v" id="d-group"></span></div>
    <div class="kv"><span class="k">프로젝트</span><span class="v" id="d-project-meta"></span></div>
    <div class="kv"><span class="k">에이전트</span><span class="v" id="d-agent"></span></div>
    <div class="kv"><span class="k">역할</span><span class="v" id="d-role"></span></div>
    <div class="kv"><span class="k">에이전트 질문</span><span class="v wrap" id="d-question"></span></div>
    <div class="actions">
      <button id="approve" type="button">승인</button>
      <button id="reject" type="button">거절</button>
      <div id="interaction"></div>
    </div>
  </div>

  <div class="live-label"><span><i id="live-dot" class="live-dot"></i>작업 화면</span><span id="live-meta"></span></div>
  <div class="live-tools">
    <button id="live-latest" type="button">최신으로</button>
    <button id="live-smaller" type="button">A-</button>
    <button id="live-larger" type="button">A+</button>
    <button id="live-wrap" type="button">줄바꿈</button>
    <button id="live-full" type="button">전체화면</button>
  </div>
  <div id="screen-wrap"><pre id="screen"></pre></div>

  <div class="send-box">
    <div class="target">보낼 곳: <b id="send-target"></b></div>
    <textarea id="prompt-text" placeholder="이 에이전트에게 보낼 내용을 입력하세요..."></textarea>
    <div class="hint" id="prompt-count"></div>
    <div class="actions">
      <button id="send">전송</button>
      <button id="edit-toggle">이름 바꾸기</button>
    </div>
  </div>

  <div id="result-box">
    <div class="kv" id="result-row"><span class="k">결과</span><span class="v" id="d-result">-</span></div>
    <pre id="result-text"></pre>
    <div class="actions">
      <button id="result-view" type="button">결과 보기</button>
      <button id="result-copy" type="button">결과 복사</button>
    </div>
    <div class="hint" id="result-msg"></div>
  </div>

  <div class="d-meta">
    <div class="kv"><span class="k">상태</span><span class="v" id="d-status"></span></div>
    <div class="kv"><span class="k">현재 작업</span><span class="v wrap" id="d-activity"></span></div>
    <div class="kv"><span class="k">확인할 일</span><span class="v" id="d-attention"></span></div>
  </div>

  <details class="advanced">
    <summary>고급 정보</summary>
    <div class="d-meta">
      <div class="kv"><span class="k">화면 이름</span><span class="v" id="d-title"></span></div>
      <div class="kv"><span class="k">컴퓨터</span><span class="v" id="d-host"></span></div>
      <div class="kv"><span class="k">위치</span><span class="v wrap" id="d-location"></span></div>
      <div class="kv"><span class="k">이름 출처</span><span class="v wrap" id="d-sense"></span></div>
      <div class="actions"><button id="focus-pc" type="button">컴퓨터에서 이 작업 열기</button></div>
    </div>
  </details>

  <div id="edit">
    <label for="edit-task-name">작업 이름</label>
    <input id="edit-task-name" maxlength="80">
    <div class="hint" id="edit-task-name-auto"></div>
    <details class="advanced">
      <summary>Agent · 역할</summary>
      <label for="edit-agent">Agent</label>
      <input id="edit-agent" maxlength="80">
      <div class="hint" id="edit-agent-auto"></div>
      <label for="edit-role">역할</label>
      <select id="edit-role">
        <option value="">역할 없음</option>
        <option value="orchestrator">조율</option>
        <option value="planner">계획</option>
        <option value="builder">구현</option>
        <option value="reviewer">검수</option>
        <option value="qa">확인</option>
        <option value="dogfood">실사용</option>
        <option value="e2e">전체 흐름</option>
      </select>
      <details class="advanced">
        <summary>터미널 관리</summary>
        <label for="edit-title">실제 화면 이름</label>
        <input id="edit-title" maxlength="80">
      </details>
    </details>
    <div class="actions"><button id="edit-reset">자동 이름으로 복원</button></div>
    <div class="actions">
      <button id="edit-save">저장</button>
      <button id="edit-cancel">취소</button>
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
  var sourceGone = $("source-gone");
  var detail = $("detail"), screenEl = $("screen"), screenWrap = $("screen-wrap"), liveMeta = $("live-meta");
  var PROMPT_LIMIT = __PROMPT_LIMIT__;
  var promptText = $("prompt-text"), sendBtn = $("send"), toast = $("toast");
  var promptCount = $("prompt-count");
  function promptTooLongMessage() {
    return "프롬프트가 너무 깁니다. 조금 줄여주세요. (" + PROMPT_LIMIT.toLocaleString("ko-KR") + "자까지 가능)";
  }
  function refreshPromptCount() {
    var n = promptText.value.length;
    promptCount.textContent = n.toLocaleString("ko-KR") + " / " + PROMPT_LIMIT.toLocaleString("ko-KR");
  }
  promptText.addEventListener("input", refreshPromptCount);
  refreshPromptCount();
  var resultBox = $("result-box"), resultText = $("result-text"), resultMsg = $("result-msg");
  var resultFingerprint = "";
  var editBox = $("edit");

  var panes = {};        // key -> last status row
  var collapsedFolders = {};
  var collapsedWindows = {};
  var current = null;    // key of the open detail, or null
  var sending = false;
  var screenTimer = null, listTimer = null;
  var screenInterval = SCREEN_POLL_MIN_MS;
  var screenInFlight = false;
  var lastScreenText = null;
  var followLive = true;
  var liveFont = 12;
  var liveWrap = false;

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

  function postGroupAction(body) {
    return api("/api/groups/action", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      if (res.status === 200 && res.body && res.body.ok) {
        showToast("작업 묶음을 업데이트했습니다.");
        pollList();
      } else showToast("작업 묶음을 업데이트하지 못했습니다.");
    }).catch(function () { showToast("Tower에 연결할 수 없습니다."); });
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
          startTimers();
        } else {
          err.textContent = "코드가 틀렸거나 만료되었습니다.";
        }
      })
      .catch(function () { err.textContent = "Tower에 연결할 수 없습니다."; });
  });

  function statusLabel(s) {
    return { WORKING: "작업 중", WAITING: "대기", IDLE: "대기", UNKNOWN: "확인 불가", DEAD: "종료" }[s] || "확인 불가";
  }

  function attentionMark(p) {
    if (!p) return "";
    if (p.attention === "approval_required") return "! 승인 필요";
    var interaction = p.interaction || {};
    if (interaction.type === "choice" || (interaction.type === "confirm" && (interaction.options || []).length)) return "? 선택 필요";
    if (interaction.type === "text" || p.attention === "input_required" || p.status === "WAITING") return "? 답변 필요";
    if (p.attention === "error") return "! 오류";
    return "";
  }

  function statusDot(s) {
    return { WORKING: "●", WAITING: "!", IDLE: "○", UNKNOWN: "◇", DEAD: "×" }[s] || "◇";
  }

  function formatDuration(sec) {
    sec = Math.max(0, sec | 0);
    if (sec < 60) return sec + "초";
    var m = (sec / 60) | 0;
    if (m < 60) return m + "분";
    var h = (m / 60) | 0;
    var rest = m % 60;
    return h + "시간" + (rest ? " " + rest + "분" : "");
  }

  // -- list view ----------------------------------------------------

  function primaryState(p) {
    if (!p) return { mark: "◇", label: "확인 불가" };
    if (p.attention === "approval_required") return { mark: "!", label: "승인 필요" };
    var attention = attentionMark(p);
    if (attention) return { mark: attention.slice(0, 1), label: attention.slice(2) };
    if (p.result_state === "ready") return { mark: "✓", label: "새 결과" };
    if (p.status === "WAITING") return { mark: "?", label: "답변 필요" };
    if (p.status === "WORKING") return { mark: "●", label: "작업 중" };
    if (p.status === "IDLE") return { mark: "○", label: "대기" };
    if (p.status === "DEAD") return { mark: "×", label: "종료" };
    return { mark: "◇", label: "확인 불가" };
  }

  function executionState(p) {
    if (p.status === "WORKING") return "● 작업 중";
    if (p.status === "IDLE") return "○ 대기";
    if (p.status === "DEAD") return "× 종료";
    if (p.status === "WAITING") return "○ 대기";
    return "◇ 확인 불가";
  }

  function renderCounts(items) {
    var counts = { approval: 0, choice: 0, answer: 0, result: 0, working: 0 };
    items.forEach(function (p) {
      if (p.offline) return;
      if (p.attention === "approval_required") counts.approval += 1;
      else if (p.attention === "input_required" || p.status === "WAITING") {
        if (attentionMark(p).indexOf("선택") >= 0) counts.choice += 1;
        else counts.answer += 1;
      }
      else if (p.result_state === "ready") counts.result += 1;
      else if (p.status === "WORKING") counts.working += 1;
    });
    var parts = [];
    if (counts.approval) parts.push("! 승인 필요 " + counts.approval);
    if (counts.choice) parts.push("? 선택 필요 " + counts.choice);
    if (counts.answer) parts.push("? 답변 필요 " + counts.answer);
    if (counts.result) parts.push("✓ 새 결과 " + counts.result);
    if (counts.working) parts.push("● 작업 중 " + counts.working);
    $("counts").textContent = parts.join("   ");
  }

  var worksetDrafts = {};
  var worksetSignature = "";
  function renderWorksets(payload) {
    var savedBox = $("saved-worksets"), templateBox = $("work-templates");
    var signature = JSON.stringify([payload.saved_work || [], payload.work_templates || [], payload.execution_targets || []]);
    if (signature === worksetSignature) return;
    worksetSignature = signature;
    savedBox.innerHTML = "";
    templateBox.innerHTML = "";
    function renderItems(items, isTemplate, parent) {
      (items || []).forEach(function (item) {
        var key = item.kind + ":" + item.id;
        var draft = worksetDrafts[key] || (worksetDrafts[key] = { agents: {} });
        var card = document.createElement("details");
        card.className = "workset-card";
        var summary = document.createElement("summary");
        summary.textContent = item.name || "작업";
        card.appendChild(summary);
        var meta = document.createElement("div");
        meta.className = "workset-meta";
        meta.textContent = (item.project ? "프로젝트 · " + item.project + " · " : "") + (item.member_count || 0) + "개 작업";
        card.appendChild(meta);
        (item.members || []).forEach(function (member) {
          var line = document.createElement("div");
          line.className = "workset-member";
          line.textContent = (member.role_label || member.role || "작업") + " · " + member.agent;
          card.appendChild(line);
        });
        var pathLabel = document.createElement("label");
        pathLabel.textContent = "프로젝트 경로";
        var pathInput = document.createElement("input");
        pathInput.type = "text";
        pathInput.autocomplete = "off";
        pathInput.placeholder = isTemplate ? "예: ~/Projects/MyProject" : "저장된 경로 사용 (변경 시 입력)";
        pathInput.value = typeof draft.project_path === "string" ? draft.project_path : "";
        pathInput.addEventListener("input", function () { draft.project_path = pathInput.value; });
        card.appendChild(pathLabel); card.appendChild(pathInput);

        var targetLabel = document.createElement("label");
        targetLabel.textContent = "실행 위치";
        var targetSelect = document.createElement("select");
        (payload.execution_targets || [{ id: "auto", name: "자동" }]).forEach(function (target) {
          var option = document.createElement("option");
          option.value = target.id;
          option.textContent = target.name;
          option.selected = (draft.execution_target || "auto") === target.id;
          targetSelect.appendChild(option);
        });
        targetSelect.addEventListener("change", function () { draft.execution_target = targetSelect.value; });
        card.appendChild(targetLabel); card.appendChild(targetSelect);

        var agentSelects = [];
        var agentChoices = ["Codex", "Claude", "Cursor", "OpenCode", "Grok", "Shell"];
        (item.members || []).forEach(function (member) {
          var label = document.createElement("label");
          label.textContent = (member.role_label || member.role || "작업") + " · Agent";
          var select = document.createElement("select");
          var choices = agentChoices.slice();
          if (choices.indexOf(member.agent) < 0) choices.unshift(member.agent);
          choices.forEach(function (agent) {
            var option = document.createElement("option");
            option.value = agent;
            option.textContent = agent;
            option.selected = (draft.agents[member.id] || member.agent) === agent;
            select.appendChild(option);
          });
          select.addEventListener("change", function () { draft.agents[member.id] = select.value; });
          card.appendChild(label); card.appendChild(select);
          agentSelects.push({ member: member, select: select });
        });

        var start = document.createElement("button");
        start.type = "button";
        start.textContent = "구성 확인 후 시작";
        start.addEventListener("click", function () {
          var path = pathInput.value.trim();
          if (isTemplate && !path) { showToast("프로젝트 경로를 입력하세요."); return; }
          var agentOverrides = {};
          agentSelects.forEach(function (choice) {
            if (choice.select.value !== choice.member.agent) agentOverrides[choice.member.id] = choice.select.value;
          });
          var preview = [item.name, item.project ? "프로젝트 · " + item.project : "", path ? "경로 · " + path : "저장된 경로 사용"];
          agentSelects.forEach(function (choice) {
            preview.push((choice.member.role_label || choice.member.role || "작업") + " · " + choice.select.value);
          });
          if (!window.confirm(preview.filter(Boolean).join("\n"))) return;
          api("/api/worksets/start", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id: item.id, kind: item.kind,
              project_path: path || undefined,
              execution_target: targetSelect.value === "auto" ? undefined : targetSelect.value,
              agents: agentOverrides
            })
          }).then(function (res) {
            if (res.status === 200 && res.body && res.body.ok) {
              delete worksetDrafts[key];
              showToast("작업 묶음을 시작했습니다: " + res.body.group_name);
              pollList();
            } else showToast((res.body && res.body.error) || "작업을 시작하지 못했습니다.");
          }).catch(function () { showToast("Tower에 연결할 수 없습니다."); });
        });
        card.appendChild(start);
        parent.appendChild(card);
      });
    }
    renderItems(payload.saved_work, false, savedBox);
    renderItems(payload.work_templates, true, templateBox);
    if (!savedBox.children.length) savedBox.textContent = "저장된 작업이 없습니다.";
    if (!templateBox.children.length) templateBox.textContent = "작업 템플릿이 없습니다.";
  }

  function render(payload) {
    list.innerHTML = "";
    renderWorksets(payload);
    panes = {};
    renderCounts(payload.panes || []);
    var paneRows = payload.panes || [];
    paneRows.forEach(function (p) {
      panes[p.key] = p;
    });

    function appendPaneCard(p, parent) {
      if (p.offline) {
        var off = document.createElement("div");
        off.className = "offline";
        off.textContent = "연결할 수 없습니다.";
        parent.appendChild(off);
        return;
      }

      var card = document.createElement("div");
      card.className = "card" + (attentionMark(p) ? " attention waiting" : "");
      card.addEventListener("click", function () { openDetail(p.key); });

      var row1 = document.createElement("div");
      row1.className = "row1";
      var dot = document.createElement("div");
      dot.className = "dot " + p.status;
      var proj = document.createElement("div");
      proj.className = "project";
      proj.textContent = p.display_name || p.task_name || p.project || "터미널";
      var agent = document.createElement("div");
      agent.className = "agent";
      agent.textContent = [p.role_label, p.agent].filter(Boolean).join(" · ");
      row1.appendChild(dot); row1.appendChild(proj); row1.appendChild(agent);
      var projectMeta = document.createElement("div");
      projectMeta.className = "project-meta";
      projectMeta.textContent = [p.project ? "프로젝트 · " + p.project : "",
        p.work_group_name ? "작업 묶음 · " + p.work_group_name : ""].filter(Boolean).join(" · ");

      var statusLine = document.createElement("div");
      statusLine.className = "status-line";
      var state = primaryState(p);
      statusLine.textContent = state.mark + " " + state.label + " · " + formatDuration(p.duration_seconds);
      card.appendChild(row1);
      if (projectMeta.textContent) card.appendChild(projectMeta);
      card.appendChild(statusLine);
      parent.appendChild(card);
    }

    function appendGroupControls(p, group, allGroups, parent) {
      if (p.offline || !p.target_id) return;
      var tools = document.createElement("div");
      tools.className = "group-tools";
      var select = document.createElement("select");
      select.setAttribute("aria-label", "작업 묶음으로 이동");
      var placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "작업 옮기기";
      select.appendChild(placeholder);
      if (group) {
        var ungroup = document.createElement("option");
        ungroup.value = "__ungroup__";
        ungroup.textContent = "작업 묶음에서 빼기";
        select.appendChild(ungroup);
      }
      (allGroups || []).forEach(function (candidate) {
        if (group && candidate.group_id === group.group_id) return;
        var option = document.createElement("option");
        option.value = candidate.group_id;
        option.textContent = candidate.display_name;
        select.appendChild(option);
      });
      select.addEventListener("click", function (event) { event.stopPropagation(); });
      select.addEventListener("change", function (event) {
        event.stopPropagation();
        if (!select.value) return;
        postGroupAction({
          action: "move", group_id: group ? group.group_id : "", target_id: p.target_id,
          destination_group_id: select.value === "__ungroup__" ? null : select.value
        });
      });
      tools.appendChild(select);
      if (group) {
        [["up", "↑"], ["down", "↓"]].forEach(function (entry) {
          var button = document.createElement("button");
          button.type = "button";
          button.textContent = entry[1];
          button.setAttribute("aria-label", entry[0] === "up" ? "앞으로" : "뒤로");
          button.addEventListener("click", function (event) {
            event.stopPropagation();
            postGroupAction({ action: "reorder", group_id: group.group_id, target_id: p.target_id, direction: entry[0] });
          });
          tools.appendChild(button);
        });
      }
      tools.addEventListener("click", function (event) { event.stopPropagation(); });
      parent.appendChild(tools);
    }

    var groups = payload.groups || [];
    var groupByTarget = {};
    groups.forEach(function (g) {
      (g.member_target_ids || []).forEach(function (target) { groupByTarget[target] = g; });
    });

    function appendWindow(windowAsset, parent) {
      var section = document.createElement("details");
      section.className = "work-window";
      section.open = collapsedWindows[windowAsset.window_ref] !== undefined
        ? collapsedWindows[windowAsset.window_ref] : !windowAsset.collapsed;
      section.addEventListener("toggle", function () { collapsedWindows[windowAsset.window_ref] = !section.open; });
      var summary = document.createElement("summary");
      var name = document.createElement("span");
      name.className = "window-name";
      name.textContent = windowAsset.display_name || "터미널";
      summary.appendChild(name);
      var countLine = document.createElement("span");
      countLine.className = "window-summary";
      countLine.textContent = formatGroupSummary(windowAsset.summary_counts || {});
      summary.appendChild(countLine);
      section.appendChild(summary);
      var content = document.createElement("div");
      content.className = "window-content";
      if (windowAsset.stale) {
        var stale = document.createElement("div");
        stale.className = "offline";
        stale.textContent = "◇ 확인할 수 없는 작업 화면";
        content.appendChild(stale);
      } else {
        (windowAsset.task_target_ids || []).forEach(function (target) {
          var member = paneRows.find(function (item) { return item.target_id === target || item.key === target; });
          if (!member) return;
          appendPaneCard(member, content);
          appendGroupControls(member, groupByTarget[target] || null, groups, content);
        });
      }
      section.appendChild(content);
      parent.appendChild(section);
    }

    function appendFolder(folder, parent) {
      var section = document.createElement("details");
      section.className = "work-folder";
      section.open = collapsedFolders[folder.folder_id] !== undefined
        ? collapsedFolders[folder.folder_id] : !folder.collapsed;
      section.addEventListener("toggle", function () { collapsedFolders[folder.folder_id] = !section.open; });
      var summary = document.createElement("summary");
      var name = document.createElement("span");
      name.className = "folder-name";
      name.textContent = folder.display_name;
      summary.appendChild(name);
      var countLine = document.createElement("span");
      countLine.className = "folder-summary";
      countLine.textContent = formatGroupSummary(folder.summary_counts || {});
      summary.appendChild(countLine);
      section.appendChild(summary);
      (folder.windows || []).forEach(function (windowAsset) { appendWindow(windowAsset, section); });
      parent.appendChild(section);
    }

    var workspace = payload.workspace || {};
    (workspace.folders || []).forEach(function (folder) { appendFolder(folder, list); });
    if ((workspace.unfiled_windows || []).length) {
      appendFolder({ folder_id: "__unfiled__", display_name: "기타",
        summary_counts: (workspace.unfiled_windows || []).reduce(function (sum, item) {
          Object.keys(sum).forEach(function (key) { sum[key] += (item.summary_counts || {})[key] || 0; });
          return sum;
        }, { error: 0, attention: 0, working: 0, result: 0, idle: 0, unavailable: 0 }),
        windows: workspace.unfiled_windows }, list);
    }

    var groupLayouts = document.createElement("details");
    groupLayouts.className = "advanced";
    var groupLayoutsTitle = document.createElement("summary");
    groupLayoutsTitle.textContent = "작업 묶음 보기 배치";
    groupLayouts.appendChild(groupLayoutsTitle);
    groups.forEach(function (g) {
      var tools = document.createElement("div");
      tools.className = "group-tools";
      var label = document.createElement("span");
      label.textContent = g.display_name;
      tools.appendChild(label);
      var layout = document.createElement("select");
      layout.setAttribute("aria-label", g.display_name + " 보기 배치");
      [["focus", "집중"], ["split-2", "좌우 2분할"], ["grid-4", "2×2 보기"], ["main-plus-side", "메인 + 사이드"]].forEach(function (entry) {
        var option = document.createElement("option");
        option.value = entry[0]; option.textContent = entry[1];
        option.selected = entry[0] === (g.layout || "focus");
        layout.appendChild(option);
      });
      layout.addEventListener("change", function () {
        postGroupAction({ action: "layout", group_id: g.group_id, layout: layout.value });
      });
      tools.appendChild(layout);
      groupLayouts.appendChild(tools);
    });
    if (groups.length) list.appendChild(groupLayouts);

    if (!paneRows.length && !groups.length && !(workspace.folders || []).length) {
      var empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "아직 실행 중인 작업이 없습니다. PC Tower에서 작업을 시작하거나 저장된 작업을 열어주세요.";
      list.appendChild(empty);
    }

    if (current) fillDetailMeta(panes[current]);
  }

  function formatGroupSummary(c) {
    var items = [];
    if (c.error) items.push("! 오류 " + c.error);
    if (c.attention) items.push("! 확인 필요 " + c.attention);
    if (c.working) items.push("● 작업 중 " + c.working);
    if (c.result) items.push("✓ 새 결과 " + c.result);
    if (c.idle) items.push("○ 대기 " + c.idle);
    if (c.unavailable) items.push("◇ 확인 불가 " + c.unavailable);
    return items.join(" · ") || "포함된 작업 없음";
  }

  function pollList() {
    // A status poll from the pair screen has no token. Its 401 used to
    // arrive after a successful pair and hide the dashboard again.
    if (document.hidden || !token) return;
    var sentToken = token;
    api("/api/status").then(function (res) {
      if (token !== sentToken) return;
      if (res.status === 401) {
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
        token = null;
        stopTimers();
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

  function renderInteraction(p) {
    var box = $("interaction");
    if (!box) return;
    box.innerHTML = "";
    var item = p && p.interaction;
    if (!item) return;
    if (item.type === "unknown" || (item.type === "confirm" && !(item.options || []).some(function (option) {
      return option && option.safe && option.key;
    }))) {
      box.textContent = "확인이 필요합니다. 실제 터미널에서 선택해주세요.";
      return;
    }
    var options = item.options || [];
    options.forEach(function (option) {
      if (!option || !option.safe || !option.key) return;
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = option.label || option.key;
      button.addEventListener("click", function () {
        postAttentionBody({ action: "option", key: option.key });
      });
      box.appendChild(button);
    });
    if (item.input_allowed) {
      var answer = document.createElement("button");
      answer.type = "button";
      answer.textContent = "답변하기";
      answer.addEventListener("click", function () {
        var text = $("prompt-text") ? $("prompt-text").value : "";
        postAttentionBody({ action: "text", text: text });
      });
      box.appendChild(answer);
    }
  }

  function postAttentionBody(body) {
    if (!current) return;
    var key = current;
    api(paneUrl(key, "attention"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      if (current !== key) return;
      if (res.status === 200 && res.body && res.body.ok) {
        if (body.action === "text" && promptText && promptText.value === body.text) promptText.value = "";
      } else {
        setBanner((res.body && res.body.error) || "요청이 전달되지 않았습니다.");
      }
    });
  }

  function fillDetailMeta(p) {
    if (!p) return;
    $("d-project").textContent = p.display_name || p.task_name || p.project || "새 작업";
    $("d-group-row").style.display = p.work_group_name ? "flex" : "none";
    $("d-group").textContent = p.work_group_name || "";
    $("d-project-meta").textContent = p.project || "(이름 없음)";
    $("d-agent").textContent = p.agent || "-";
    $("d-role").textContent = p.role_label || "역할 없음";
    var badgeBox = $("d-badges");
    badgeBox.innerHTML = "";
    var seen = {};
    function addBadge(text) {
      if (!text || seen[text]) return;
      seen[text] = true;
      var chip = document.createElement("span");
      chip.textContent = text;
      badgeBox.appendChild(chip);
    }
    var lead = primaryState(p);
    addBadge(lead.mark + " " + lead.label);
    var attention = attentionMark(p);
    if (attention) addBadge(attention);
    if (p.result_state === "ready") addBadge("✓ 새 결과");
    addBadge(executionState(p));
    $("d-status").textContent = executionState(p) + " · " + formatDuration(p.duration_seconds);
    $("d-activity").textContent = p.activity || "-";
    $("d-title").textContent = p.pane_title || "-";
    $("d-host").textContent = p.execution_host || p.host || "-";
    var mark = attentionMark(p);
    $("d-attention").textContent = mark || "-";
    $("d-question").textContent = p.attention_prompt || "-";
    $("approve").style.display = p.approval_known ? "block" : "none";
    renderInteraction(p);
    $("reject").style.display = p.reject_known ? "block" : "none";
    var where = "실행 위치: " + (p.execution_host || p.host || "-");
    where += "\\n터미널 위치: " + (p.tmux_host || p.host || "-") + " → Session " + (p.session || "-");
    if (p.window_index != null && p.window_index !== "") {
      where += "\\nWindow " + (p.window_id ? p.window_id + " " : "") + p.window_index + ": " + (p.window_name || "");
    }
    if (p.pane_id) where += "\\nPane " + p.pane_id + (p.pane_index != null && p.pane_index !== "" ? " · index " + p.pane_index : "");
    if (p.transport === "ssh") where += "\\nSSH → " + (p.transport_target || p.execution_host || "");
    else if (p.remote) where += "\\n이 호스트의 tmux (읽기 전용)";
    $("d-location").textContent = where;
    var senseAgent = p.agent_source || "-";
    var senseProject = p.project_source || "-";
    if (senseAgent === "override" && p.auto_agent_source) senseAgent = "override→" + p.auto_agent_source;
    if (senseProject === "override" && p.auto_project_source) senseProject = "override→" + p.auto_project_source;
    $("d-sense").textContent = senseAgent + " / " + senseProject;
    $("focus-pc").style.display = p.remote ? "none" : "block";
    $("edit-toggle").style.display = p.remote ? "none" : "block";
    var resultLabel = $("d-result");
    if (p.result_state === "ready") resultLabel.textContent = "✓ 새 결과";
    else if (p.result_state === "read") resultLabel.textContent = "확인한 결과";
    else resultLabel.textContent = "-";
    resultBox.classList.toggle("open", p.result_state === "ready" || p.result_state === "read");
    // The target is always visible right above the textarea.
    $("send-target").textContent = (p.display_name || p.task_name || p.project || "새 작업") + " · " + (p.agent || "-");
  }

  function openDetail(key) {
    var p = panes[key];
    if (!p) return;
    current = key;
    fillDetailMeta(p);
    screenEl.textContent = "";
    screenEl.classList.remove("stale");
    lastScreenText = null;
    followLive = true;
    liveMeta.textContent = "실시간";
    promptText.value = "";
    resultText.textContent = "";
    resultMsg.textContent = "";
    resultFingerprint = "";
    editBox.classList.remove("open");
    main.classList.add("hidden");
    detail.classList.add("open");
    if (p.remote) {
      screenEl.textContent = "다른 컴퓨터의 작업은 이름과 상태만 볼 수 있습니다. 작업 화면은 이 컴퓨터에서 실행 중인 작업만 볼 수 있습니다.";
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

  screenWrap.addEventListener("scroll", function () {
    var gap = screenWrap.scrollHeight - screenWrap.scrollTop - screenWrap.clientHeight;
    followLive = gap < 24;
    if (!followLive && current) liveMeta.textContent = "이전 내용 보기";
  });
  $("live-latest").addEventListener("click", function () {
    followLive = true;
    screenWrap.scrollTop = screenWrap.scrollHeight;
    if (current) liveMeta.textContent = "실시간";
  });
  $("live-smaller").addEventListener("click", function () {
    liveFont = Math.max(10, liveFont - 1);
    screenEl.style.fontSize = liveFont + "px";
  });
  $("live-larger").addEventListener("click", function () {
    liveFont = Math.min(22, liveFont + 1);
    screenEl.style.fontSize = liveFont + "px";
  });
  $("live-wrap").addEventListener("click", function () {
    liveWrap = !liveWrap;
    screenEl.style.whiteSpace = liveWrap ? "pre-wrap" : "pre";
  });
  $("live-full").addEventListener("click", function () {
    screenWrap.classList.toggle("full");
  });

  function postAttention(action) {
    if (!current) return;
    var p = panes[current];
    if (!p || p.remote) return;
    var key = current;
    api(paneUrl(key, "attention"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action })
    }).then(function (res) {
      if (current !== key) return;
      if (res.status === 200 && res.body && res.body.ok) {
        showToast(action === "approve" ? "승인 키를 보냈습니다." : "거절 키를 보냈습니다.");
        return;
      }
      if (res.body && res.body.error === "approval_unknown") {
        showToast("실제 작업 화면에서 승인해 주세요.");
        return;
      }
      showToast((res.body && res.body.error) || "처리하지 못했습니다.");
    }).catch(function () { showToast("처리하지 못했습니다."); });
  }

  $("approve").addEventListener("click", function () { postAttention("approve"); });
  $("reject").addEventListener("click", function () { postAttention("reject"); });

  $("focus-pc").addEventListener("click", function () {
    if (!current || sending) return;
    var p = panes[current];
    if (!p || p.remote) return;
    var key = current;
    api(paneUrl(key, "focus"), { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).then(function (res) {
      if (current !== key) return;
      if (res.status === 200 && res.body && res.body.ok) {
        showToast("컴퓨터에서 이 작업을 열었습니다.");
        return;
      }
      showToast((res.body && res.body.error) || "이동하지 못했습니다.");
    }).catch(function () {
      showToast("이동하지 못했습니다.");
    });
  });

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
          screenEl.textContent = text || "(화면이 비어 있어요)";
          lastScreenText = text;
          if (followLive) screenWrap.scrollTop = screenWrap.scrollHeight;
        }
        screenEl.classList.remove("stale");
      var pace = "응답 " + rtt + "밀리초" + (res.body.truncated ? " · 일부 표시" : "");
        liveMeta.textContent = (followLive ? "실시간" : "이전 내용 보기") + " · " + pace;
        $("live-dot").className = "live-dot";
      } else if (res.body && res.body.error === "source_session_missing") {
        closeDetail();
        showSourceGone(res.body.session);
        return;
      } else {
        screenEl.classList.add("stale");
        liveMeta.textContent = "화면을 불러오지 못했습니다.";
        if (res.body && (res.body.error === "not_found" || res.body.error === "stale")) {
          showToast("이 작업 화면은 더 이상 없습니다.");
          closeDetail();
          return;
        }
      }
      scheduleScreen();
    }).catch(function () {
      screenInFlight = false;
      if (current !== key) return;
      screenEl.classList.add("stale");
      liveMeta.textContent = "연결이 끊겼습니다.";
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
        resultMsg.textContent = "결과를 가져오지 못했습니다.";
        return null;
      }
      resultFingerprint = res.body.fingerprint || "";
      if (!res.body.complete || !res.body.text) {
        resultText.textContent = "";
        resultMsg.textContent = "최신 결과 전체를 찾지 못했습니다";
        return null;
      }
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
        resultMsg.textContent = "복사할 결과가 없습니다.";
        return;
      }
      var done = function () {
        resultMsg.textContent = "✓ 최신 결과 전체를 이 브라우저에 복사했습니다";
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
    if (text.length > PROMPT_LIMIT) { showToast(promptTooLongMessage()); return; }
    if (p.status === "DEAD") { showToast("이 작업은 끝났습니다."); return; }
    if (p.attention === "input_required") {
      var who = p.agent || "에이전트";
      var asked = p.attention_prompt || "";
      if (!window.confirm(who + "에게 답변을 보냅니다.\\n" + asked + "\\n\\n전송할까요?")) return;
    }
    if (p.agent === "Shell" || p.agent === "SSH") {
      if (!window.confirm("이 화면은 일반 명령 창입니다. 입력한 내용이 그대로 실행됩니다. 보낼까요?")) return;
    }

    sending = true;
    setSendState("보내는 중...", true);
    api("/api/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pane_key: p.key, text: text, project: p.project, agent: p.agent }),
    }).then(function (res) {
      sending = false;
      if (res.status === 200 && res.body.ok && res.body.submitted) {
        // Only a confirmed submit clears the box.
        promptText.value = "";
        setSendState("제출 확인됨", false, 2000);
        showToast("제출 확인됨");
        pollScreen();
      } else if (res.status === 200 && res.body.ok) {
        setSendState("제출 확인 실패", false, 2500);
        showToast("입력은 전송했지만 제출 여부를 확인하지 못했습니다. 입력한 글은 그대로 두었습니다.");
        pollScreen();
      } else {
        setSendState("전송", false);
        if (res.body.error === "prompt_too_long") showToast(promptTooLongMessage());
        else showToast("전송 실패 (" + (res.body.error || res.status) + ")");
      }
    }).catch(function () {
      sending = false;
      setSendState("전송", false);
      showToast("Tower에 연결할 수 없습니다.");
    });
  });

  var sendStateTimer = null;
  function setSendState(label, disabled, revertMs) {
    sendBtn.textContent = label;
    sendBtn.disabled = disabled;
    if (sendStateTimer) { clearTimeout(sendStateTimer); sendStateTimer = null; }
    if (revertMs) {
      sendStateTimer = setTimeout(function () { sendBtn.textContent = "전송"; sendBtn.disabled = false; }, revertMs);
    }
  }

  // -- identity edit: same OverrideStore as the PC's E menu -----------

  $("edit-toggle").addEventListener("click", function () {
    var p = panes[current];
    if (!p) return;
    $("edit-task-name").value = p.display_name || p.task_name || "";
    $("edit-role").value = p.role || "";
    $("edit-agent").value = p.agent || "";
    $("edit-title").value = p.pane_title || "";
    $("edit-task-name-auto").textContent = p.suggested_name ? "자동 추천: " + p.suggested_name : "";
    $("edit-agent-auto").textContent = p.auto_agent ? "자동 이름: " + p.auto_agent : "";
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
    var taskName = $("edit-task-name").value.trim();
    var agent = $("edit-agent").value.trim();
    var role = $("edit-role").value;
    var title = $("edit-title").value.trim();
    if (taskName !== (p.display_name || p.task_name || "")) body.task_name = taskName || null;
    if (agent !== (p.agent || "")) body.agent = agent || null;
    if (role !== (p.role || "")) body.role = role || null;
    if (title !== (p.pane_title || "")) body.title = title || null;
    if (Object.keys(body).length === 0) { editBox.classList.remove("open"); return; }
    postIdentity(body, "저장했습니다. PC Tower에도 반영됩니다.");
  });

  $("edit-reset").addEventListener("click", function () {
    postIdentity({ task_name: null }, "자동 이름을 적용했습니다.");
  });

  // -- polling lifecycle: nothing runs while the page is hidden -------

  function startTimers() {
    if (!token) return;
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
    if (document.hidden) {
      stopTimers();
      $("live-dot").className = "live-dot paused";
      if (current) liveMeta.textContent = "일시정지";
    } else {
      $("live-dot").className = "live-dot";
      startTimers();
    }
  });

  if (token) {
    pairScreen.style.display = "none";
    main.style.display = "block";
    startTimers();
  }
})();
</script>
</body>
</html>
""".replace("__LIST_POLL_MS__", str(LIST_POLL_MS)).replace(
    "__SCREEN_POLL_MIN_MS__", str(SCREEN_POLL_MIN_MS)
).replace("__SCREEN_POLL_MAX_MS__", str(SCREEN_POLL_MAX_MS)).replace(
    "__SCREEN_POLL_SLOW_MS__", str(SCREEN_POLL_SLOW_MS)
).replace("__PROMPT_LIMIT__", str(MAX_PROMPT_CHARS))

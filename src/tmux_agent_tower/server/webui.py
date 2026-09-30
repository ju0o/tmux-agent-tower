"""The one static page Tower Remote serves.

Deliberately not a terminal emulator: no scrollback streaming, no
WebSocket (2-3s polling is enough for a status view -- see
docs/ROADMAP.md's Tower Remote section). Inline CSS/JS, no build step,
no external CDN -- the whole point is that it opens instantly on a phone
browser over plain LAN HTTP.
"""

from __future__ import annotations

PAGE_HTML = """<!doctype html>
<html lang="en">
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
  #banner {
    display: none; background: var(--dead); color: #fff; font-size: 13px;
    padding: 8px 12px; border-radius: 8px; margin-bottom: 10px; text-align: center;
  }
  #banner.show { display: block; }
  #banner.warn { background: var(--waiting); color: #1a1200; }
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
  .card.selected { border-color: var(--working); }
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
  #panel {
    position: fixed; left: 0; right: 0; bottom: 0; background: var(--card);
    border-top: 1px solid var(--line); padding: 12px; display: none;
  }
  #panel.open { display: block; }
  #panel .target { font-size: 12px; color: var(--dim); margin-bottom: 6px; }
  #panel textarea {
    width: 100%; min-height: 80px; background: #11141a; color: var(--fg);
    border: 1px solid var(--line); border-radius: 8px; padding: 10px; font-size: 16px; line-height: 1.4;
    font-family: inherit; resize: vertical;
  }
  #panel .actions { display: flex; gap: 8px; margin-top: 8px; }
  #panel button {
    flex: 1; min-height: 46px; padding: 10px; border-radius: 8px; border: none;
    font: inherit; font-size: 16px; font-weight: 600;
  }
  #send { background: var(--working); color: #041018; }
  #send:disabled { opacity: .6; }
  #cancel { background: var(--line); color: var(--fg); }
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
    font-size: 13px; display: none;
  }
</style>
</head>
<body>
<div id="toast"></div>

<div id="pair-screen">
  <h1 style="margin:0 0 6px">Tmux Agent Tower</h1>
  <p style="color:var(--dim); font-size:13px; margin:0">Enter the pairing code shown on the PC's terminal.</p>
  <input id="pair-code" inputmode="numeric" maxlength="6" placeholder="000000">
  <button id="pair-submit">Pair</button>
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

<div id="panel">
  <div class="target" id="panel-target"></div>
  <textarea id="prompt-text" placeholder="Type a prompt to send to this pane..." maxlength="4000"></textarea>
  <div class="actions">
    <button id="cancel">Cancel</button>
    <button id="send">Send</button>
  </div>
</div>

<script>
(function () {
  "use strict";

  var TOKEN_KEY = "tower_remote_token";
  var token = null;
  try { token = localStorage.getItem(TOKEN_KEY); } catch (e) {}

  var pairScreen = document.getElementById("pair-screen");
  var main = document.getElementById("main");
  var list = document.getElementById("list");
  var banner = document.getElementById("banner");
  var sourceGone = document.getElementById("source-gone");
  var sourceGoneName = document.getElementById("source-gone-name");
  var panel = document.getElementById("panel");
  var panelTarget = document.getElementById("panel-target");
  var promptText = document.getElementById("prompt-text");
  var sendBtn = document.getElementById("send");
  var toast = document.getElementById("toast");
  var selected = null; // {key, project, agent}
  var sending = false;

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
    // The pane list is cleared on purpose: leaving remote-host rows up
    // would look like a healthy Tower that simply has no local panes.
    list.innerHTML = "";
    panel.classList.remove("open");
    selected = null;
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

  document.getElementById("pair-submit").addEventListener("click", function () {
    var code = document.getElementById("pair-code").value.trim();
    var err = document.getElementById("pair-error");
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
          poll();
        } else {
          err.textContent = "Invalid or expired code.";
        }
      })
      .catch(function () { err.textContent = "Could not reach Tower."; });
  });

  function statusLabel(s) {
    return { WORKING: "working", WAITING: "needs input", IDLE: "idle", UNKNOWN: "unknown", DEAD: "exited" }[s] || s;
  }

  function formatDuration(sec) {
    sec = Math.max(0, sec | 0);
    if (sec < 60) return sec + "s";
    var m = (sec / 60) | 0;
    if (m < 60) return m + "m";
    var h = (m / 60) | 0;
    return h + "h";
  }

  function render(payload) {
    list.innerHTML = "";
    var byHost = {};
    var order = [];
    payload.panes.forEach(function (p) {
      if (!byHost[p.host]) { byHost[p.host] = []; order.push(p.host); }
      byHost[p.host].push(p);
    });

    order.forEach(function (host) {
      var h = document.createElement("div");
      h.className = "host";
      h.textContent = host;
      list.appendChild(h);

      byHost[host].forEach(function (p) {
        if (p.offline) {
          var off = document.createElement("div");
          off.className = "offline";
          off.textContent = "(unreachable)";
          list.appendChild(off);
          return;
        }

        var card = document.createElement("div");
        var cls = "card";
        if (p.status === "WAITING") cls += " attention";
        if (selected && selected.key === p.key) cls += " selected";
        card.className = cls;
        card.addEventListener("click", function () { openPanel(p); });

        var row1 = document.createElement("div");
        row1.className = "row1";
        var dot = document.createElement("div");
        dot.className = "dot " + p.status;
        var proj = document.createElement("div");
        proj.className = "project";
        proj.textContent = p.project || "(no name)";
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

        list.appendChild(card);
      });
    });
  }

  function openPanel(p) {
    if (p.status === "DEAD") { showToast("This pane has exited."); return; }
    selected = { key: p.key, project: p.project, agent: p.agent };
    panelTarget.textContent = "To: " + (p.project || "(no name)") + " / " + (p.agent || "");
    promptText.value = "";
    panel.classList.add("open");
  }

  document.getElementById("cancel").addEventListener("click", function () {
    panel.classList.remove("open");
    selected = null;
  });

  sendBtn.addEventListener("click", function () {
    if (!selected || sending) return;
    var text = promptText.value;
    if (!text.trim()) return;

    sending = true;
    sendBtn.disabled = true;
    var target = selected;

    api("/api/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pane_key: target.key, text: text,
        project: target.project, agent: target.agent,
      }),
    }).then(function (res) {
      sending = false;
      sendBtn.disabled = false;
      if (res.status === 200 && res.body.ok) {
        showToast("Sent.");
        panel.classList.remove("open");
        selected = null;
      } else {
        showToast("Could not send (" + (res.body.error || res.status) + ").");
      }
    }).catch(function () {
      sending = false;
      sendBtn.disabled = false;
      showToast("Could not reach Tower.");
    });
  });

  var polling = false;
  function poll() {
    if (polling) return;
    polling = true;
    api("/api/status").then(function (res) {
      polling = false;
      if (res.status === 401) {
        try { localStorage.removeItem(TOKEN_KEY); } catch (e) {}
        token = null;
        main.style.display = "none";
        pairScreen.style.display = "flex";
        return;
      }
      setBanner(null); // a successful poll clears any prior "unreachable" state
      if (res.body && res.body.error === "source_session_missing") {
        showSourceGone(res.body.session);
        return;
      }
      if (res.status !== 200 || !res.body || !res.body.panes) {
        setBanner("Tower returned an error (" + ((res.body && res.body.error) || res.status) + ").");
        return;
      }
      sourceGone.classList.remove("show");
      render(res.body);
    }).catch(function () {
      polling = false;
      // The PC's Tower process is unreachable (stopped, network dropped,
      // phone lost Wi-Fi) -- say so plainly rather than silently leaving
      // a stale screen up with no indication it's out of date.
      setBanner("Tower unreachable — last update above may be stale.");
    });
  }

  if (token) {
    pairScreen.style.display = "none";
    main.style.display = "block";
    poll();
  }
  setInterval(poll, 2500);
})();
</script>
</body>
</html>
"""

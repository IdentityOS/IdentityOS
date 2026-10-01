"""
runtime/health_server.py — localhost-only Aster Control endpoint.

Read paths (Tailnet-private, no per-request auth — same posture as the
dashboard they extend; polling them never invokes a model):

  GET /health            sanitized presence JSON
  GET /status            mobile dashboard (Overview tab renders server-side)
  GET /api/presence      presence JSON (alias)
  GET /api/activity      sanitized provenance/notification timeline
  GET /api/relationships sanitized relationships
  GET /api/capabilities  installed capabilities + per-skill permission state
  GET /api/messages      principal conversation history (sanitized envelopes)
  GET /manifest.json     PWA manifest (no tracking, local icon only)
  GET /icon.svg          original Aster mark (local asset, no third parties)

State-changing paths (require principal verification — see _principal_verified):

  POST /api/messages     submit a principal message to the SAME persistent
                         identity; stored RECEIVED, processed by the operator
                         loop, never faked.

Principal verification = Tailnet Host match + exact Tailscale-User-Login match
+ anti-CSRF request marker. Tailscale Serve injects the login header for
genuine Tailnet viewers; direct-local requests cannot satisfy the Host rule,
so forged headers from localhost curl are rejected. Funnel is never used.

Zero model calls on any path in this process. Binds localhost only by
default. Only sanitized fields leave the box: no topics, phone numbers,
credentials, tokens, standing secrets, message contents (outside the
principal's own conversation), or chain-of-thought.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

from core.operations.presence import PresenceStore
from runtime.persistence import get_backend

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 15
MESSAGES_POLL_SECONDS = 4
ACTIVITY_POLL_SECONDS = 20
SLOW_POLL_SECONDS = 60
MAX_POST_BYTES = 16 * 1024
MAX_POSTS_PER_MINUTE = 20

_post_minutes: dict[str, int] = {}
_post_minutes_lock = threading.Lock()


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_dashboard(view: dict[str, Any]) -> str:
    """Render the mobile-first presence dashboard from a sanitized view.

    The page paints server-rendered values immediately, then refreshes its
    data every ``REFRESH_SECONDS`` via ``fetch('/health')`` with in-place DOM
    updates (no full-page reload, no frameworks, no model calls).
    """
    status = _esc(view.get("status") or "unknown")
    health = _esc(view.get("health") or "unknown")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0f14">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Aster">
<meta name="mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<title>Aster · {{status}}</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 16px 16px 32px; background: #0b0f14; color: #e8eef4;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif; }}
.wrap {{ max-width: 560px; margin: 0 auto; }}
.head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }}
h1 {{ font-size: 26px; margin: 6px 0 2px; letter-spacing: .2px; }}
.sub {{ color: #93a1b3; font-size: 13px; }}
.pill {{ display: inline-flex; align-items: center; gap: 8px; font-weight: 700; font-size: 15px;
  padding: 8px 14px; border-radius: 999px; background: #1a2230; border: 1px solid #2b3a55; }}
.dot {{ width: 12px; height: 12px; border-radius: 50%; background: #8a94a6; }}
.h-online .dot {{ background: #34d17b; box-shadow: 0 0 8px #34d17b; }}
.h-degraded .dot {{ background: #ff6b4a; box-shadow: 0 0 8px #ff6b4a; }}
.h-offline .dot, .h-unknown .dot {{ background: #4a5568; }}
.card {{ background: #131a24; border: 1px solid #223047; border-radius: 14px;
  padding: 14px 16px; margin-top: 12px; }}
.label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px; color: #93a1b3; }}
.value {{ font-size: 17px; margin-top: 4px; line-height: 1.45; word-break: break-word; }}
.big {{ font-size: 20px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
.foot {{ margin-top: 14px; color: #93a1b3; font-size: 12px; text-align: center; }}
.warn {{ color: #ffb02e; }}
nav.tabs {{ display: flex; gap: 6px; margin-top: 14px; overflow-x: auto; }}
nav.tabs button {{ flex: 1 0 auto; background: #131a24; color: #93a1b3; border: 1px solid #223047;
  border-radius: 10px; padding: 10px 8px; font-size: 13px; font-weight: 600; }}
nav.tabs button.on {{ color: #e8eef4; border-color: #34d17b; }}
section.tab {{ display: none; }}
section.tab.on {{ display: block; }}
.ev {{ border-left: 3px solid #2b3a55; padding: 6px 0 6px 12px; margin: 10px 0; }}
.ev .t {{ font-size: 12px; color: #93a1b3; }}
.ev .c {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #5aa9ff; }}
.msg {{ border-radius: 14px; padding: 10px 14px; margin: 10px 0; max-width: 92%; line-height: 1.45; }}
.msg.in {{ background: #1d3a5f; margin-left: auto; }}
.msg.out {{ background: #16241d; border: 1px solid #234034; }}
.msg .meta {{ font-size: 11px; color: #93a1b3; margin-top: 6px; }}
.msg .st {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 8px;
  border-radius: 999px; background: #223047; margin-top: 6px; }}
.st-processing {{ color: #ffb02e; }} .st-completed, .st-sent {{ color: #34d17b; }}
.st-deferred, .st-permission_required, .st-failed {{ color: #ff6b4a; }}
form.composer {{ display: flex; gap: 8px; margin-top: 12px; position: sticky; bottom: 0;
  background: #0b0f14; padding: 10px 0; }}
form.composer textarea {{ flex: 1; background: #131a24; color: #e8eef4; border: 1px solid #223047;
  border-radius: 12px; padding: 10px 12px; font-size: 16px; resize: none; }}
form.composer button {{ background: #34d17b; color: #06210f; border: 0; border-radius: 12px;
  padding: 0 18px; font-size: 16px; font-weight: 700; }}
.err {{ color: #ff6b4a; font-size: 13px; margin-top: 8px; }}
.connline {{ display: flex; align-items: center; justify-content: space-between; margin-top: 10px; }}
.conn {{ font-size: 11px; font-weight: 700; letter-spacing: 1.2px; padding: 3px 10px; border-radius: 999px; background: #1a2230; color: #93a1b3; }}
.conn.live {{ color: #34d17b; }} .conn.reconnecting {{ color: #ffb02e; }} .conn.offline {{ color: #ff6b4a; }}
#needs-you {{ font-size: 12px; color: #ffb02e; font-weight: 700; }}
.navbadge {{ display: inline-block; min-width: 20px; text-align: center; background: #ff6b4a; color: #fff;
  font-size: 12px; font-weight: 700; border-radius: 999px; padding: 1px 6px; margin-left: 6px; }}
.btn {{ background: #34d17b; color: #06210f; border: 0; border-radius: 12px; padding: 12px 18px;
  font-size: 16px; font-weight: 700; margin-top: 10px; min-height: 44px; }}
.btn.secondary {{ background: #1a2230; color: #e8eef4; border: 1px solid #2b3a55; }}
body {{ padding-bottom: calc(32px + env(safe-area-inset-bottom)); }}
form.composer {{ padding-bottom: calc(10px + env(safe-area-inset-bottom)); }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <div>
      <h1 id="identity">{_esc(view.get("identity") or "Aster")}</h1>
      <div class="sub">IdentityOS operator · AI identity</div>
    </div>
    <div class="pill st-{status} h-{health}" id="pill"><span class="dot"></span><span id="status">{status.upper()}</span></div>
  </div>
  <div class="connline"><span id="conn" class="conn unknown">CONNECTING…</span><span id="needs-you"></span></div>
  <div class="card" id="sw-update" style="display:none"><div class="label">App update</div>
    <div class="value">A new Aster app version is ready.</div>
    <button class="btn" id="sw-reload">Update now</button></div>
  <nav class="tabs">
    <button data-tab="home" class="on">Home</button>
    <button data-tab="activity">Activity</button>
    <button data-tab="relationships">People</button>
    <button data-tab="capabilities">Skills</button>
    <button data-tab="work">Work</button>
    <button data-tab="messages">Messages</button>
    <button data-tab="notifications">Alerts<span id="nav-badge" class="navbadge" style="display:none"></span></button>
  </nav>
  <section class="tab on" id="sec-home">
  <div class="card"><div class="label">Activity</div><div class="value big" id="activity">{_esc(view.get("activity") or "—")}</div></div>
  <div class="card"><div class="label">Current objective</div><div class="value" id="objective">{_esc(view.get("current_objective") or "—")}</div></div>

  <div class="grid">
    <div class="card"><div class="label">Last heartbeat</div><div class="value" id="heartbeat">—</div></div>
    <div class="card"><div class="label">Uptime</div><div class="value" id="uptime">—</div></div>
  </div>

  <div class="card"><div class="label">Last meaningful action</div><div class="value" id="action">{_esc(view.get("last_meaningful_action") or "None recorded yet")}</div><div class="sub" id="action-age"></div></div>
  <div class="card"><div class="label">Next</div><div class="value" id="next">{_esc(view.get("next_planned_action") or "—")}</div><div class="sub" id="next-in"></div></div>

  <div class="grid">
    <div class="card"><div class="label">Service</div><div class="value" id="service">—</div></div>
    <div class="card"><div class="label">Culture Commons</div><div class="value" id="commons">—</div></div>
  </div>
  <div class="grid">
    <div class="card"><div class="label">Notifications</div><div class="value" id="notifications">—</div></div>
    <div class="card"><div class="label">Opportunities</div><div class="value" id="opportunities">—</div></div>
  </div>
  <div class="card"><div class="label">Capabilities</div><div class="value" id="capabilities">—</div><div class="sub" id="capabilities-sub"></div></div>
  <div class="card"><div class="label">Identity</div><div class="value" id="profile-name">Aster · AI Identity · IdentityOS</div><div class="sub" id="profile-detail">Loading…</div></div>
  </section>
  <section class="tab" id="sec-activity">
    <div class="card"><div class="label">Recent activity</div><div id="timeline"><div class="sub">Loading…</div></div></div>
  </section>
  <section class="tab" id="sec-relationships">
    <div class="card"><div class="label">Relationships</div><div id="rel-list"><div class="sub">Loading…</div></div></div>
  </section>
  <section class="tab" id="sec-capabilities">
    <div class="card"><div class="label">Capabilities</div><div id="cap-list"><div class="sub">Loading…</div></div></div>
  </section>
  <section class="tab" id="sec-work"><div class="card"><div class="label">Delegated work</div><div id="work-list">No delegated work.</div></div></section>
  <section class="tab" id="sec-messages">
    <div class="card"><div class="label">Conversation with Aster</div><div id="msg-list"><div class="sub">Loading…</div></div>
      <form class="composer" id="composer">
        <textarea id="composer-text" rows="2" maxlength="4000" placeholder="Message Aster…"></textarea>
        <button type="submit">Send</button>
      </form>
      <div class="err" id="composer-err"></div>
    </div>
  </section>
  <section class="tab" id="sec-notifications">
    <div class="card"><div class="label">Notifications</div>
      <div class="value" id="push-state">Checking…</div>
      <div class="sub" id="push-detail"></div>
      <div id="push-actions"><button class="btn" id="enable-push" style="display:none">Enable Notifications</button><button class="btn secondary" id="push-refresh" style="display:none">Refresh push registration</button></div>
    </div>
    <div class="card"><div class="label">Needs you</div><div id="attention-list"><div class="sub">Loading…</div></div></div>
    <div class="card"><div class="label">Notification center</div><div id="notify-list"><div class="sub">Loading…</div></div></div>
  </section>

  <div class="foot"><span id="updated">Loading…</span> · auto-refreshes</div>
</div>
<script>
(function () {{
  "use strict";
  var last = null;
  function $(id) {{ return document.getElementById(id); }}
  function ago(iso) {{
    if (!iso) return "unknown";
    var s = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
    if (s < 5) return "just now";
    if (s < 90) return s + " seconds ago";
    var m = Math.floor(s / 60);
    if (m < 90) return m + (m === 1 ? " minute ago" : " minutes ago");
    var h = Math.floor(m / 60);
    if (h < 48) return h + (h === 1 ? " hour ago" : " hours ago");
    return Math.floor(h / 24) + " days ago";
  }}
  function ahead(iso) {{
    if (!iso) return "";
    var s = Math.floor((Date.parse(iso) - Date.now()) / 1000);
    if (s <= 0) return "due now";
    if (s < 90) return "in " + s + " seconds";
    var m = Math.floor(s / 60);
    if (m < 90) return "in " + m + (m === 1 ? " minute" : " minutes");
    return "in " + Math.floor(m / 60) + " hours";
  }}
  function dur(sec) {{
    if (sec == null || sec < 0) return "—";
    sec = Math.floor(sec);
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    if (h > 0) return h + "h " + m + "m";
    if (m > 0) return m + "m " + (sec % 60) + "s";
    return sec + "s";
  }}
  function apply(v) {{
    last = v;
    document.title = "Aster · " + (v.status || "unknown");
    var pill = $("pill");
    pill.className = "pill st-" + (v.status || "unknown") + " h-" + (v.health || "unknown");
    $("status").textContent = String(v.status || "unknown").toUpperCase();
    $("identity").textContent = v.identity || "Aster";
    $("activity").textContent = (v.status || "") + " — " + (v.activity || "No activity recorded");
    $("objective").textContent = v.current_objective || "—";
    $("action").textContent = v.last_meaningful_action || "None recorded yet";
    $("next").textContent = v.next_planned_action || "—";
    var pid = v.operator_pid ? " · pid " + v.operator_pid : "";
    $("service").textContent = (v.service_state || "unknown") + pid;
    $("commons").textContent = v.commons_standing || "unknown";
    $("notifications").textContent = "ntfy · " + (v.notification_transport_health || "unknown");
    $("opportunities").textContent = String(v.opportunity_count == null ? 0 : v.opportunity_count);
    getJSON("/api/profile").then(function (p) {{
      var pr = p.profile || {{}};
      if (pr.display_name) $("profile-name").textContent = pr.display_name + " · " + (pr.identity_type || "") + " · " + (pr.system || "");
      var bits = [];
      if (pr.email) bits.push(pr.email);
      if (pr.disclosure) bits.push(pr.disclosure);
      if (bits.length) $("profile-detail").textContent = bits.join(" · ");
    }}).catch(function () {{}});
    var sum = v.capability_summary || {{}};
    var limited = sum.limited || [];
    var disp = v.capability_display || "unknown";
    $("capabilities").textContent = (sum.healthy == null ? 0 : sum.healthy) + " healthy · " + limited.length + " limited (" + disp + ")";
    $("capabilities-sub").textContent = limited.length ? "Limited: " + limited.join(", ") : "";
    tick();
  }}
  function tick() {{
    if (!last) return;
    $("heartbeat").textContent = ago(last.last_heartbeat);
    var up = null;
    if (last.started_at && last.operator_pid) {{
      up = (Date.now() - Date.parse(last.started_at)) / 1000;
    }}
    $("uptime").textContent = up == null ? "—" : dur(up);
    $("action-age").textContent = last.last_meaningful_action ? ago(last.last_meaningful_action_at) : "";
    var ni = ahead(last.next_check_at);
    $("next-in").textContent = ni;
  }}
  async function refresh() {{
    var my = ++refreshSeq;
    try {{
      var r = await fetch("/health", {{ cache: "no-store" }});
      if (!r.ok) throw new Error("http " + r.status);
      var v = await r.json();
      if (my !== refreshSeq) return;
      apply(v);
      $("updated").textContent = "Last updated " + new Date().toLocaleTimeString();
      $("updated").className = "";
      setConn("live");
      try {{
        var snap = JSON.stringify({{h: last ? last.last_heartbeat : null, s: last ? last.status : null}});
        localStorage.setItem("aster-last", snap + "|" + Date.now());
      }} catch (e) {{}}
    }} catch (e) {{
      var cached = null;
      try {{ cached = localStorage.getItem("aster-last"); }} catch (ex) {{}}
      if (cached) {{
        $("updated").textContent = "Aster Control cannot currently reach IdentityOS. Last update " +
          new Date(parseInt(cached.split("|")[1], 10)).toLocaleTimeString();
      }} else {{
        $("updated").textContent = "Aster Control cannot currently reach IdentityOS.";
      }}
      $("updated").className = "warn";
      setConn("offline");
    }}
  }}
  var activeTab = "home";
  var ROUTES = {{"home": "home", "activity": "activity", "people": "relationships",
    "relationships": "relationships", "skills": "capabilities", "capabilities": "capabilities",
    "work": "work", "messages": "messages", "notifications": "notifications", "alerts": "notifications"}};
  function route() {{
    var h = (location.hash || "").replace(/^#[\\/]?/, "");
    var parts = h.split("?");
    var tab = ROUTES[(parts[0] || "").toLowerCase()] || "home";
    var params = {{}};
    if (parts[1]) {{
      parts[1].split("&").forEach(function (kv) {{
        var pair = kv.split("=");
        params[decodeURIComponent(pair[0] || "")] = decodeURIComponent(pair[1] || "");
      }});
    }}
    if (tab !== activeTab) showTab(tab);
    if (params.opened) confirmOpened(params.opened);
  }}
  function showTab(name) {{
    activeTab = name;
    var btns = document.querySelectorAll("nav.tabs button");
    for (var bi = 0; bi < btns.length; bi++) {{
      btns[bi].className = btns[bi].getAttribute("data-tab") === name ? "on" : "";
    }}
    var secs = document.querySelectorAll("section.tab");
    for (var si = 0; si < secs.length; si++) {{
      secs[si].className = "tab" + (secs[si].id === "sec-" + name ? " on" : "");
    }}
    pull(name, true);
  }}
  var navBtns = document.querySelectorAll("nav.tabs button");
  for (var ni = 0; ni < navBtns.length; ni++) {{
    (function (b) {{
      b.addEventListener("click", function () {{ showTab(b.getAttribute("data-tab")); }});
    }})(navBtns[ni]);
  }}
  function el(tag, cls, text) {{
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }}
  function fmtTime(iso) {{
    if (!iso) return "";
    try {{ return new Date(Date.parse(iso)).toLocaleString(); }} catch (e) {{ return ""; }}
  }}
  async function getJSON(path) {{
    var r = await fetch(path, {{ cache: "no-store" }});
    if (!r.ok) throw new Error("http " + r.status);
    return r.json();
  }}
  function urlBase64ToUint8Array(base64String) {{
    // PushManager.subscribe requires the application key as a BufferSource,
    // never a base64 string: a string key silently binds the subscription to
    // the wrong (or no) VAPID identity and the push service rejects every
    // send (live Apple BadJwtToken finding).
    var padding = "=".repeat((4 - base64String.length % 4) % 4);
    var base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = window.atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }}
  var CLIENT_BUILD = "2026-09-25c";
  function statusLabel(s) {{
    var m = {{"received": "Received", "queued": "Queued for Aster",
      "processing": "Aster is working on it…", "completed": "Completed",
      "deferred": "Deferred", "permission_required": "Needs your approval",
      "failed": "Failed", "sent": "Sent"}};
    return m[s] || s || "";
  }}
  async function pullMessages() {{
    var my = ++messagesSeq;
    var mm = await getJSON("/api/messages?limit=50");
    if (my !== messagesSeq) return;
    var ml = $("msg-list"); ml.textContent = "";
    var msgs = mm.messages || [];
    if (!msgs.length) ml.appendChild(el("div", "sub", "No messages yet. Say hello."));
    for (var i = 0; i < msgs.length; i++) {{
      var msg = msgs[i];
      var mine = msg.direction === "inbound";
      var bubble = el("div", "msg " + (mine ? "in" : "out"), msg.body || "");
      var meta = [fmtTime(msg.sent_at || msg.received_at || msg.created_at)];
      if (!mine && msg.via && msg.via.model) meta.push("via " + msg.via.model);
      else if (!mine && msg.via && msg.via.adapter) meta.push("via " + msg.via.adapter);
      bubble.appendChild(el("div", "meta", meta.join(" · ")));
      if (msg.runtime_evidence) {{
        var details = el("details", "sub");
        details.appendChild(el("summary", "", "Runtime evidence"));
        details.appendChild(el("div", "", "Observed " + fmtTime(msg.runtime_evidence.observed_at)));
        details.appendChild(el("div", "", msg.runtime_evidence.guard === "fallback" ? "Runtime report substituted for an unsupported model response." : "Structured claims checked against runtime state."));
        bubble.appendChild(details);
      }}
      if (mine) {{
        var st = el("div", "st st-" + msg.status,
          statusLabel(msg.status) + (msg.detail ? " — " + msg.detail : ""));
        bubble.appendChild(st);
      }}
      ml.appendChild(bubble);
    }}
  }}
  async function pull(name, now) {{
    if (document.hidden && !now) return;
    try {{
      if (name === "activity") {{
        var a = await getJSON("/api/activity?limit=30");
        var tl = $("timeline"); tl.textContent = "";
        var progress = a.autonomy || {{}};
        var cycle = progress.latest || {{}};
        var overview = el("div", "card");
        overview.appendChild(el("div", "big", "Autonomous cycle · " + (cycle.result || "No cycle recorded")));
        overview.appendChild(el("div", "sub", fmtTime(cycle.completed_at)));
        overview.appendChild(el("div", "label", "Objective"));
        overview.appendChild(el("div", "", cycle.objective || "Not recorded"));
        overview.appendChild(el("div", "label", "Last meaningful progress"));
        var lastProgress = progress.last_meaningful_progress;
        overview.appendChild(el("div", "", lastProgress ? lastProgress.summary + " · " + ago(lastProgress.at) : "No meaningful progress recorded in cycle evidence yet."));
        overview.appendChild(el("div", "label", "This cycle"));
        overview.appendChild(el("div", "", (cycle.new_information || []).join("; ") || ((cycle.actions_completed || []).length ? cycle.actions_completed.length + " objective action(s) completed." : "No new information or completed objective work recorded.")));
        overview.appendChild(el("div", "label", "Waiting for"));
        (progress.waiting || []).forEach(function(b) {{
          overview.appendChild(el("div", "", (b.principal_action_required ? "Principal decision if needed: " : "System waiting: ") + b.description));
          overview.appendChild(el("div", "sub", b.retry_condition + (b.next_eligible_retry ? " · eligible " + fmtTime(new Date(b.next_eligible_retry * 1000).toISOString()) : " · no timed retry")));
        }});
        overview.appendChild(el("div", "label", "Next eligible action"));
        (cycle.next_eligible_actions || []).forEach(function(n) {{ overview.appendChild(el("div", "", n.description)); }});
        var resources = cycle.resources || {{}};
        overview.appendChild(el("div", "sub", "Suppressed operations: " + (resources.suppressed_operations || 0) + " · principal model requests: " + (resources.model_calls || 0) + " · Commons observation calls: " + (resources.surface_reads || 0)));
        overview.appendChild(el("div", "sub", resources.measurement_scope || ""));
        overview.appendChild(el("div", "sub", "Liveness is separate from progress. " + (cycle.safety || "")));
        tl.appendChild(overview);
        var recent = (progress.cycles || []).filter(function(c) {{ return c.cycle_id !== cycle.cycle_id && c.result === "PROGRESS"; }});
        if (recent.length) {{
          var history = el("div", "card");
          history.appendChild(el("div", "label", "Recent meaningful outcomes"));
          recent.slice(0, 3).forEach(function(c) {{ history.appendChild(el("div", "", fmtTime(c.completed_at) + " · " + (c.last_meaningful_progress || {{}}).summary)); }});
          tl.appendChild(history);
        }}
        var details = el("details", "card");
        details.appendChild(el("summary", "", "Detailed provenance and repeated observations"));
        tl.appendChild(details);
        tl = details;
        var evs = a.events || [];
        if (!evs.length) tl.appendChild(el("div", "sub", "No activity recorded yet."));
        for (var i = 0; i < evs.length; i++) {{
          var ev = evs[i];
          var d = el("div", "ev");
          d.appendChild(el("div", "t", fmtTime(ev.at)));
          d.appendChild(el("div", "c", ev.category || ""));
          d.appendChild(el("div", "", ev.description || ""));
          if (ev.count > 1) d.appendChild(el("div", "sub", "Repeated " + ev.count + " times · first " + fmtTime(ev.first_at) + " · last " + fmtTime(ev.at)));
          if (ev.outcome) d.appendChild(el("div", "sub", ev.outcome));
          tl.appendChild(d);
        }}
      }} else if (name === "work") {{
        var ww = await getJSON("/api/work");
        var wl = $("work-list"); wl.textContent = "";
        if (!(ww.jobs || []).length) wl.appendChild(el("div", "sub", "No delegated work."));
        (ww.specifications || []).forEach(function(s) {{
          var card = el("div", "ev");
          card.appendChild(el("div", "big", "Specification · " + s.provider));
          card.appendChild(el("div", "", s.state + " · revision " + s.revision));
          card.appendChild(el("div", "sub", s.job ? "Job " + s.job : "No job or payment authorized by this exchange."));
          wl.appendChild(card);
        }});
        (ww.jobs || []).forEach(function(j) {{
          var card = el("div", "ev");
          card.appendChild(el("div", "big", j.provider + " · " + j.skill));
          card.appendChild(el("div", "", j.state + " · " + (j.price ? j.price + " IDC" : "FREE")));
          card.appendChild(el("div", "sub", j.pricing_reason || "Not quoted"));
          card.appendChild(el("div", "sub", "Acceptance: " + j.acceptance));
          card.appendChild(el("div", "sub", "Job " + j.id));
          if (j.artifact) card.appendChild(el("div", "sub", "Artifact " + j.artifact));
          wl.appendChild(card);
        }});
      }} else if (name === "relationships") {{
        var rr = await getJSON("/api/relationships");
        var rl = $("rel-list"); rl.textContent = "";
        var rels = rr.relationships || [];
        if (!rels.length) rl.appendChild(el("div", "sub", "No relationships yet."));
        for (var m = 0; m < rels.length; m++) {{
          var rel = rels[m];
          var card = el("div", "ev");
          card.appendChild(el("div", "c", rel.type || rel.state || ""));
          card.appendChild(el("div", "big", rel.name || ""));
          var bits = [];
          if (rel.where) bits.push(rel.where);
          if (rel.state) bits.push(rel.state);
          bits.push(rel.interactions + " interaction" + (rel.interactions === 1 ? "" : "s"));
          if (rel.last_interaction) bits.push("last " + ago(rel.last_interaction));
          card.appendChild(el("div", "sub", bits.join(" · ")));
          for (var n = 0; n < (rel.notes || []).length; n++) {{
            card.appendChild(el("div", "sub", rel.notes[n]));
          }}
          if (rel.pending) card.appendChild(el("div", "", "Pending: " + rel.pending));
          rl.appendChild(card);
        }}
      }} else if (name === "capabilities") {{
        var cc = await getJSON("/api/capabilities");
        var cl = $("cap-list"); cl.textContent = "";
        var caps = cc.capabilities || [];
        if (!caps.length) cl.appendChild(el("div", "sub", "No verified installed capabilities in this view."));
        for (var p = 0; p < caps.length; p++) {{
          var cap = caps[p];
          var cc0 = el("div", "ev");
          cc0.appendChild(el("div", "c", cap.installed ? "installed" : "not installed"));
          cc0.appendChild(el("div", "big", cap.name || cap.id));
          if (cap.state && cap.state !== "INSTALLED_EXECUTABLE") cc0.appendChild(el("div", "sub", cap.state.replaceAll("_", " ").toLowerCase()));
          if (cap.description) cc0.appendChild(el("div", "sub", cap.description));
          for (var q = 0; q < (cap.skills || []).length; q++) {{
            var sk = cap.skills[q];
            cc0.appendChild(el("div", sk.allowed ? "" : "warn",
              (sk.executable === true ? "✓ " : (!sk.allowed ? "Permission required · " : (sk.state === "INSTALLED_PROVIDER_UNAVAILABLE" ? "Provider unavailable · " : sk.state === "INSTALLED_DEPENDENCY_UNAVAILABLE" ? "Dependency unavailable · " : sk.state === "INSTALLED_MISCONFIGURED" ? "Configuration required · " : (sk.last_verified_at ? "Not recently verified · " : "Readiness unknown · ")))) + sk.name));
            if (sk.last_verified_at) cc0.appendChild(el("div", "sub", "Last verified " + (sk.verified_effect || "call") + " · " + fmtTime(sk.last_verified_at) + "; not a guarantee of present readiness."));
            if (sk.provenance) {{
              cc0.appendChild(el("div", "sub", "Provided by " + sk.provenance.provider + " · acceptance tested by " + sk.provenance.accepted_by));
              cc0.appendChild(el("div", "sub", "Job " + sk.provenance.job + " · " + sk.provenance.fingerprint));
            }}
            if (!sk.allowed && sk.limited_explanation) {{
              cc0.appendChild(el("div", "sub", sk.limited_explanation));
            }} else if (!sk.allowed && sk.reason) {{
              cc0.appendChild(el("div", "sub", sk.reason));
            }}
          }}
          cl.appendChild(cc0);
        }}
      }} else if (name === "messages") {{
        await pullMessages();
      }} else if (name === "notifications") {{
        await pullNotifications();
        await refreshPushStatus();
      }}
    }} catch (e) {{}}
  }}
  $("composer").addEventListener("submit", async function (e) {{
    e.preventDefault();
    var box = $("composer-text");
    var err = $("composer-err");
    err.textContent = "";
    var text = box.value.trim();
    if (!text) return;
    try {{
      var r = await fetch("/api/messages", {{
        method: "POST",
        headers: {{"Content-Type": "application/json", "X-Requested-With": "AsterControl"}},
        body: JSON.stringify({{text: text}})
      }});
      var data = await r.json().catch(function () {{ return {{}}; }});
      if (!r.ok) throw new Error((data && data.error) || ("http " + r.status));
      box.value = "";
      await pullMessages();
    }} catch (ex) {{
      err.textContent = "Send failed: " + ex.message;
    }}
  }});
  async function confirmOpened(id) {{
    try {{
      await fetch("/api/notify/confirm", {{
        method: "POST",
        headers: {{"Content-Type": "application/json", "X-Requested-With": "AsterControl"}},
        body: JSON.stringify({{id: id}})
      }});
      await pull("notifications", true);
    }} catch (e) {{}}
  }}
  async function markNotifyRead(id, link) {{
    try {{
      await fetch("/api/notify/read", {{
        method: "POST",
        headers: {{"Content-Type": "application/json", "X-Requested-With": "AsterControl"}},
        body: JSON.stringify({{id: id}})
      }});
    }} catch (e) {{}}
    if (link) location.hash = link.replace("/status", "");
    await pull("notifications", true);
  }}
  function setBadge(n) {{
    var nb = $("nav-badge");
    if (n > 0) {{ nb.style.display = "inline-block"; nb.textContent = n > 9 ? "9+" : String(n); }}
    else nb.style.display = "none";
    var ny = $("needs-you");
    ny.textContent = n > 0 ? (n === 1 ? "1 thing needs you" : n + " things need you") : "";
    try {{
      if ("setAppBadge" in navigator) {{
        if (n > 0) navigator.setAppBadge(n); else navigator.clearAppBadge();
      }}
    }} catch (e) {{}}
  }}
  // Request sequencing: overlapping polls (tab switches, timer bursts on
  // mobile) may resolve out of order. A stale response must never overwrite
  // fresher state — that is what made the badge flicker.
  var notifySeq = 0, messagesSeq = 0, refreshSeq = 0;
  async function pullNotifications() {{
    var my = ++notifySeq;
    var data = await getJSON("/api/notify?limit=30");
    if (my !== notifySeq) return;
    setBadge(data.badge || 0);
    var al = $("attention-list"); al.textContent = "";
    var att = data.attention || [];
    if (!att.length) al.appendChild(el("div", "sub", "Nothing needs you right now. Aster is handling herself."));
    for (var i = 0; i < att.length; i++) {{
      (function (a) {{
        var d = el("div", "ev");
        d.appendChild(el("div", "c", (a.kind || "").replace(/_/g, " ")));
        d.appendChild(el("div", "big", a.title || ""));
        var go = el("div", "warn", "Open →");
        go.style.cursor = "pointer";
        go.addEventListener("click", function () {{
          if (a.deep_link) location.hash = a.deep_link.replace("/status", "");
        }});
        d.appendChild(go);
        al.appendChild(d);
      }})(att[i]);
    }}
    var nl = $("notify-list"); nl.textContent = "";
    var items = data.notifications || [];
    var hasUnread = items.some(function (n) {{ return !n.read; }});
    if (hasUnread) {{
      fetch("/api/notify/viewed", {{
        method: "POST",
        headers: {{"Content-Type": "application/json", "X-Requested-With": "AsterControl"}},
        body: "{{}}"
      }}).then(function (r) {{ return r.ok ? r.json() : null; }}).then(function (v) {{
        if (v && typeof v.badge === "number") setBadge(v.badge);
      }}).catch(function () {{}});
    }}
    if (!items.length) nl.appendChild(el("div", "sub", "No notifications yet."));
    for (var m = 0; m < items.length; m++) {{
      (function (n) {{
        var d = el("div", "ev");
        d.appendChild(el("div", "t", fmtTime(n.created_at)));
        d.appendChild(el("div", "c", (n.kind || "").replace(/_/g, " ")));
        d.appendChild(el("div", "", n.title || ""));
        if (n.body) d.appendChild(el("div", "sub", n.body));
        var meta = [];
        if (!n.read) meta.push("unread");
        if (n.principal_confirmed_visible) meta.push("confirmed seen");
        var ch = (n.channels || []).filter(function (c) {{ return c.channel !== "resolution"; }}).pop();
        if (ch) meta.push(ch.channel + ": " + ch.result);
        if (meta.length) d.appendChild(el("div", "sub", meta.join(" · ")));
        d.style.cursor = "pointer";
        d.addEventListener("click", function () {{ markNotifyRead(n.id, n.deep_link); }});
        nl.appendChild(d);
      }})(items[m]);
    }}
  }}
  async function refreshPushRegistration() {{
    var det = $("push-detail");
    try {{
      var reg = await navigator.serviceWorker.getRegistration();
      if (!reg || !reg.pushManager) throw new Error("no push manager");
      var old = await reg.pushManager.getSubscription();
      if (old) await old.unsubscribe();
      var keyResp = await getJSON("/api/push/public-key");
      var sub = await reg.pushManager.subscribe({{
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyResp.key)
      }});
      var r = await fetch("/api/push/subscribe", {{
        method: "POST",
        headers: {{"Content-Type": "application/json", "X-Requested-With": "AsterControl"}},
        body: JSON.stringify({{subscription: sub.toJSON(), device_label: "iPhone Home Screen " + CLIENT_BUILD}})
      }});
      if (!r.ok) throw new Error("registration rejected (http " + r.status + ")");
      await refreshPushStatus();
      det.textContent = "Registration refreshed. IdentityOS recognizes this device.";
    }} catch (e) {{
      det.textContent = "Refresh failed: " + String((e && e.message) || e);
    }}
  }}
  $("push-refresh").addEventListener("click", refreshPushRegistration);
  async function refreshPushStatus() {{
    var st = $("push-state"), det = $("push-detail"), btn = $("enable-push"),
        rfb = $("push-refresh");
    try {{
      var s = await getJSON("/api/push/status");
      if (s.available) {{
        st.textContent = "First-party push: Enabled";
        det.textContent = (s.subscriptions || 0) + " device(s) · last verified " +
          (s.last_verified ? ago(s.last_verified) : "unknown");
        btn.style.display = "none";
      }} else {{
        st.textContent = "First-party push: Not enabled";
        det.textContent = "Tap below to enable notifications on this device.";
        if ("Notification" in window && Notification.permission === "default") btn.style.display = "";
        else if ("Notification" in window && Notification.permission === "denied") {{
          det.textContent = "Notifications are blocked. Enable them in iPhone Settings → Aster.";
        }}
      }}
    }} catch (e) {{
      st.textContent = "First-party push: Unknown (offline?)";
    }}
    if ("Notification" in window && Notification.permission === "granted") {{
      rfb.style.display = "";
      try {{
        var reg = await navigator.serviceWorker.getRegistration();
        if (reg && reg.pushManager) {{
          var sub = await reg.pushManager.getSubscription();
          if (!sub) {{
            det.textContent += " (subscription missing — tap Enable Notifications)";
            btn.style.display = "";
          }}
        }}
      }} catch (e) {{}}
    }} else {{
      rfb.style.display = "none";
    }}
  }}
  var SW_VERSION = "aster-sw-v1";
  $("enable-push").addEventListener("click", async function () {{
    var st = $("push-state"), det = $("push-detail"), err = null;
    try {{
      if (!("Notification" in window)) throw new Error("notifications unsupported here");
      var perm = await Notification.requestPermission();
      if (perm !== "granted") throw new Error("permission not granted");
      var reg = await navigator.serviceWorker.register("/sw.js");
      var keyResp = await getJSON("/api/push/public-key");
      var sub = await reg.pushManager.subscribe({{
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyResp.key)
      }});
      var r = await fetch("/api/push/subscribe", {{
        method: "POST",
        headers: {{"Content-Type": "application/json", "X-Requested-With": "AsterControl"}},
        body: JSON.stringify({{subscription: sub.toJSON(), device_label: "iPhone Home Screen " + CLIENT_BUILD}})
      }});
      if (!r.ok) throw new Error("registration rejected (http " + r.status + ")");
      await refreshPushStatus();
      det.textContent = "Enabled. IdentityOS will push here when something needs you.";
    }} catch (e) {{
      st.textContent = "First-party push: Error";
      det.textContent = String((e && e.message) || e);
    }}
  }});
  function setConn(state) {{
    var c = $("conn");
    c.className = "conn " + state;
    c.textContent = state === "live" ? "LIVE" : (state === "reconnecting" ? "RECONNECTING…" : "OFFLINE");
    var box = $("composer-text");
    if (box) {{
      box.disabled = (state === "offline");
      if (state === "offline") box.placeholder = "Offline — reconnect to message Aster…";
      else box.placeholder = "Message Aster…";
    }}
  }}
  var sseOK = false;
  function startSSE() {{
    if (!("EventSource" in window)) return;
    try {{
      var es = new EventSource("/api/events");
      es.onopen = function () {{ sseOK = true; setConn("live"); }};
      es.onerror = function () {{ sseOK = false; setConn("reconnecting"); }};
      es.addEventListener("state", function (ev) {{
        try {{
          var f = JSON.parse(ev.data);
          setBadge(f.badge || 0);
          refresh();
          if (activeTab === "messages") pullMessages().catch(function () {{}});
        }} catch (e) {{}}
      }});
    }} catch (e) {{}}
  }}
  setInterval(tick, 1000);
  setInterval(function () {{ if (!document.hidden && activeTab === "home") refresh(); }}, {REFRESH_SECONDS} * 1000);
  setInterval(function () {{ if (!document.hidden && activeTab === "messages") pullMessages().catch(function () {{}}); }}, {MESSAGES_POLL_SECONDS} * 1000);
  setInterval(function () {{ if (!document.hidden && activeTab === "activity") pull("activity"); }}, {ACTIVITY_POLL_SECONDS} * 1000);
  setInterval(function () {{ if (!document.hidden && (activeTab === "relationships" || activeTab === "capabilities" || activeTab === "work")) pull(activeTab); }}, {SLOW_POLL_SECONDS} * 1000);
  setInterval(function () {{ if (!document.hidden && activeTab === "notifications") pull("notifications"); }}, {REFRESH_SECONDS} * 1000);
  function watchForUpdates() {{
    // Safe update behavior: a waiting worker never takes over on its own
    // (no skipWaiting) and reloads only happen on explicit user taps, so no
    // reload loop is possible. Surfacing updates also defeats stale cached
    // pages holding old code: the banner appears, the user reloads once.
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.getRegistration().then(function (reg) {{
      if (!reg) return;
      reg.addEventListener("updatefound", function () {{
        var worker = reg.installing;
        if (!worker) return;
        worker.addEventListener("statechange", function () {{
          if (worker.state === "installed" && navigator.serviceWorker.controller) {{
            $("sw-update").style.display = "";
          }}
        }});
      }});
    }}).catch(function () {{}});
    $("sw-reload").addEventListener("click", function () {{
      location.reload();
    }});
  }}
  window.addEventListener("hashchange", route);
  route();
  startSSE();
  watchForUpdates();
  refresh();
}})();
</script>
</body>
</html>"""


# ── control API: sanitizers ───────────────────────────────────────────────

def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _timeline(store: Any, *, limit: int = 30) -> list[dict[str, Any]]:
    """Sanitized recent activity from provenance + notifications.

    Only timestamp, category, summary, and short outcome are exposed.
    Evidence arrays and refs are deliberately excluded: they may carry
    operational details (addresses, excerpts) that have no place in the UI.
    """
    entries: list[dict[str, Any]] = []
    for item in store.list_provenance(limit=1000):
        entries.append({
            "at": item.at,
            "category": item.phase.value if hasattr(item.phase, "value") else str(item.phase),
            "description": _clip(item.summary, 280),
            "outcome": _clip(item.result, 200),
        })
    for note in store.list_notifications()[-max(1, limit):]:
        entries.append({
            "at": getattr(note, "at", ""),
            "category": f"notification:{getattr(note, 'kind', '')}",
            "description": _clip(getattr(note, "summary", ""), 280),
            "outcome": "unread" if not getattr(note, "read", True) else "read",
        })
    # Group the read model only; underlying provenance remains untouched.
    from core.services.integration import existing_store
    from datetime import datetime, timezone
    services = existing_store(store._storage)
    if services:
        identity = store.identity_id
        for event in services.rows('SELECT kind,job,created FROM events WHERE identity=? ORDER BY seq DESC LIMIT 100', (identity,)):
            entries.append({'at':datetime.fromtimestamp(event['created'],timezone.utc).isoformat(),
                'category':'service','description':event['kind'].replace('_',' '),
                'outcome':event['job'] or ''})
    entries.sort(key=lambda e: e["at"] or "", reverse=True)
    groups = {}
    for entry in entries:
        key = (entry['category'],entry['description'],entry['outcome'])
        if key not in groups:
            groups[key] = dict(entry, count=1, first_at=entry['at'])
        else:
            groups[key]['count'] += 1
            groups[key]['first_at'] = entry['at']
    return list(groups.values())[:max(1,limit)]


def _relationship_cards(store: Any) -> list[dict[str, Any]]:
    """Sanitized relationships. Email addresses are never exposed."""
    cards: list[dict[str, Any]] = []
    for rel in store.list_relationships():
        last = [t for t in (
            getattr(rel, "last_inbound_at", None),
            getattr(rel, "last_outbound_at", None),
        ) if t]
        notes = [_clip(n, 200) for n in (getattr(rel, "notes", None) or [])][-5:]
        thread_ids = list(getattr(rel, "thread_ids", None) or [])
        cards.append({
            "id": rel.id,
            "name": _clip(getattr(rel, "display_name", ""), 120) or "(unnamed)",
            "type": _clip(getattr(rel, "role", "") or getattr(rel, "purpose", ""), 160),
            "state": rel.status.value if hasattr(rel.status, "value") else str(rel.status),
            "where": _clip(getattr(rel, "organization", ""), 160),
            "first_contact": getattr(rel, "first_contacted_at", None),
            "last_interaction": max(last) if last else None,
            "interactions": len(getattr(rel, "message_ids", None) or []),
            "threads": len(thread_ids),
            "notes": notes,
            "pending": _clip(getattr(rel, "next_action", ""), 200),
        })
    cards.sort(key=lambda c: c["last_interaction"] or "", reverse=True)
    return cards


def _capability_cards(registry: Any, identity_id: str, store: Any) -> list[dict[str, Any]]:
    """Thin UI adapter over canonical read-only self-state; never reload/install."""
    from core.self_knowledge import SelfKnowledge
    snapshot = SelfKnowledge(registry._storage, identity_id).snapshot(['capabilities','jobs'])
    data = snapshot['sections']['capabilities'].get('data') or {}
    jobs = (snapshot['sections']['jobs'].get('data') or {}).get('items', [])
    cards = []
    for cap_id, provider in data.get('providers', {}).items():
        skills = []
        for name, skill in data.get('skills', {}).items():
            if skill['provider'] != cap_id: continue
            accepted = [j for j in jobs if j['verified_completed'] and j['requester'] == identity_id]
            provenance = None
            # Artifact config stays private. Match fingerprint through stored safe bundle metadata.
            if cap_id == 'service_artifacts':
                installed = registry._storage.load(identity_id, 'capabilities') or {}
                entry = next((e for e in installed.get('installed',[]) if e['id']==cap_id), {})
                digest = entry.get('config',{}).get('bundles',{}).get(name,{}).get('fingerprint')
                match = next((j for j in accepted if j['artifact']==digest), None)
                if match: provenance = {'provider':match['provider'],'job':match['id'],'fingerprint':digest,'accepted_by':identity_id}
            skills.append({'name':name,'allowed':skill['authority'],'executable':skill['executable'],
                'state':skill['state'],'classification':skill['classification'],'readiness':skill.get('readiness','UNKNOWN'),
                'permission':', '.join(skill['required_permissions']),'reason':skill['reason'],
                'limited_explanation':skill['reason'] if not skill['authority'] else '',
                'provenance':provenance,'last_verified_at':skill.get('last_verified_at'), 'verified_effect':skill.get('verified_effect')})
        cards.append({'id':cap_id,'name':cap_id,'version':provider.get('version'),'installed':True,
                      'state':provider['state'],'description':'','skills':skills,'last_used':'unknown'})
    return cards


def _message_cards(store: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """Principal conversation envelopes. Bodies belong to the principal's own
    conversation and are visible here — and nowhere else in the UI."""
    from core.operations.principal import CONTROL_CHANNEL, thread_messages

    cards: list[dict[str, Any]] = []
    for message in thread_messages(store, limit=limit):
        response_id = ""
        detail = ""
        for piece in message.evidence or []:
            if not isinstance(piece, str):
                continue
            if piece.startswith("response:"):
                response_id = piece.split(":", 1)[1]
            elif piece and not detail:
                detail = piece[:300]
        generation = dict(message.generation or {})
        cards.append({
            "id": message.id,
            "direction": message.direction.value if hasattr(message.direction, "value") else str(message.direction),
            "body": message.body or "",
            "status": message.status.value if hasattr(message.status, "value") else str(message.status),
            "created_at": message.created_at,
            "received_at": message.received_at,
            "sent_at": message.sent_at,
            "in_reply_to": message.in_reply_to or "",
            "thread_id": message.thread_id or "",
            "response_id": response_id,
            "detail": detail,
            "runtime_evidence": generation.get("grounding"),
            "via": {
                "mode": generation.get("mode", ""),
                "adapter": generation.get("adapter", ""),
                "model": generation.get("model", ""),
            },
        })
    return cards


def _communication_profile() -> dict[str, Any]:
    """Public communication identity. Contains no secrets by construction."""
    try:
        from core.operations.aster import aster_communication_identity

        identity = aster_communication_identity()
        return {
            "identity": identity.identity_name,
            "identity_type": identity.identity_type,
            "system": identity.system,
            "email": identity.email,
            "display_name": identity.display_sender(),
            "principal": identity.principal,
            "description": identity.short_description,
            "disclosure": identity.disclosure_first_contact,
            "writing_profile": "active",
            "style_invariant": "no U+2014 in Aster-authored outbound text",
        }
    except Exception:
        return {"identity": "Aster", "writing_profile": "unknown"}


# ── control API: principal verification ───────────────────────────────────

def _expected_login() -> str:
    return (os.environ.get("ASTER_BUILDER_LOGIN", "") or "").strip()


def _expected_host() -> str:
    return (os.environ.get("ASTER_TAILNET_HOST", "") or "").strip().lower()


def _principal_verified(handler: BaseHTTPRequestHandler) -> tuple[bool, str]:
    """Verify a state-changing request comes from the builder via the Tailnet.

    Three independent checks, all required:
    1. Host must be the Tailnet hostname. Tailscale Serve forwards the
       original Host; direct-local requests (Host: localhost/127.0.0.1)
       fail here even with forged identity headers.
    2. Tailscale-User-Login must exactly match the configured builder login.
       Tailscale Serve injects this for genuine Tailnet viewers.
    3. Anti-CSRF marker: browsers issuing cross-site requests cannot set
       X-Requested-With (preflight fails — we never emit CORS headers), so
       ordinary CSRF forms/fetch are rejected. Same-origin UI sets it.
    """
    host = (handler.headers.get("Host", "") or "").split(":")[0].strip().lower()
    expected_host = _expected_host()
    if not expected_host or host != expected_host:
        return False, "untrusted host"
    login = (handler.headers.get("Tailscale-User-Login", "") or "").strip()
    expected_login = _expected_login()
    if not expected_login or login != expected_login:
        return False, "principal not verified"
    if not handler.headers.get("X-Requested-With"):
        origin = (handler.headers.get("Origin", "") or handler.headers.get("Referer", ""))
        if expected_host not in origin.lower():
            return False, "missing request marker"
    return True, ""


def _post_rate_ok() -> bool:
    bucket = int(time.time() // 60)
    with _post_minutes_lock:
        _post_minutes[bucket] = _post_minutes.get(bucket, 0) + 1
        for old in [k for k in _post_minutes if k < bucket - 1]:
            del _post_minutes[old]
        return _post_minutes[bucket] <= MAX_POSTS_PER_MINUTE


# ── PWA assets ────────────────────────────────────────────────────────────

#: Pub/Sub push endpoint for Gmail users.watch. Kept off /api/ because it is
#: called by Google, not by the dashboard.
GMAIL_PUSH_PATH = "/api/gmail/push"

APP_MANIFEST = {
    "name": "Aster",
    "short_name": "Aster",
    "description": "Private Aster Control for IdentityOS",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0b0f14",
    "theme_color": "#0b0f14",
    "start_url": "/status",
    "scope": "/",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
    ],
}

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<rect width="128" height="128" rx="28" fill="#0b0f14"/>
<circle cx="64" cy="64" r="34" fill="none" stroke="#34d17b" stroke-width="6"/>
<circle cx="64" cy="64" r="12" fill="#34d17b"/>
<circle cx="64" cy="18" r="5" fill="#5aa9ff"/>
</svg>
"""

SW_VERSION = "aster-sw-v1"

SERVICE_WORKER = """/* Aster Control service worker (conservative update behavior).
 * Installed only from a user gesture (Enable Notifications). Never force
 * reloads the page, never wipes subscriptions: a waiting worker only takes
 * over on explicit user update action surfaced by the UI.
 */
var SW_VERSION = 'aster-sw-v1';
var DEEP_LINK_BASE = '/status#';

self.addEventListener('install', function (event) {
  // Do NOT skipWaiting: the active worker keeps serving until the user
  // explicitly updates, so a bad deploy can never strand the Home Screen app.
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  var title = data.title || 'Aster';
  var options = {
    body: data.body || 'Aster needs your attention.',
    tag: data.tag || 'aster-general',
    data: { deep_link: data.deep_link || '/status#/notifications' },
    badge: '/icon.svg',
    icon: '/icon.svg',
    renotify: false
  };
  var show = self.registration.showNotification(title, options);
  var badging = Promise.resolve();
  if (typeof data.badge === 'number' && data.badge >= 0 && 'setAppBadge' in self.navigator) {
    try { badging = self.navigator.setAppBadge(data.badge); } catch (e) {}
  }
  event.waitUntil(Promise.all([show, badging]));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.deep_link) || '/status#/notifications';
  var url = new URL(target, self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        try {
          var existing = new URL(list[i].url);
          if (existing.origin === self.location.origin) {
            list[i].navigate(url);
            return list[i].focus();
          }
        } catch (e) {}
      }
      return self.clients.openWindow(url);
    })
  );
});

self.addEventListener('pushsubscriptionchange', function (event) {
  // Best effort: re-subscribe WITH the current application key (a keyless
  // re-subscribe would orphan the endpoint against our VAPID identity) and
  // report. Auth headers ride the same-origin request through Tailscale
  // Serve identity injection.
  event.waitUntil(
    fetch('/api/push/public-key', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('key'); return r.json(); })
      .then(function (k) {
        return self.registration.pushManager.subscribe({
          userVisibleOnly: true, applicationServerKey: (function (k) {{ var p = '='.repeat((4 - k.length % 4) % 4); var b = (k + p).replace(/-/g, '+').replace(/_/g, '/'); var r = atob(b); var o = new Uint8Array(r.length); for (var i = 0; i < r.length; i++) o[i] = r.charCodeAt(i); return o; }})(k.key)
        });
      })
      .then(function (sub) {
        return fetch('/api/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'AsterControl' },
          body: JSON.stringify(sub.toJSON())
        });
      }).catch(function () {})
  );
});
"""


class _HealthHandler(BaseHTTPRequestHandler):
    presence_store: Optional[PresenceStore] = None
    #: Set by serve() when Gmail push is configured and active. The push
    #: endpoint is 503 without it rather than pretending to accept mail.
    gmail_push_service: Any = None

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default log noise; serve() prints its own startup line.
        pass

    def _backend(self):
        """Build store/registry accessors bound to this server's storage."""
        presence = self.presence_store
        storage = presence._storage if presence is not None else None
        identity_id = presence.identity_id if presence is not None else "aster"
        return storage, identity_id

    def _notify_manager(self):
        from core.operations.notify import NotificationManager

        storage, identity_id = self._backend()
        if storage is None:
            return None
        return NotificationManager(storage, identity_id)

    def _secret_store(self):
        from core.secrets.store import SecretStore, default_secret_store_dir

        try:
            return SecretStore(default_secret_store_dir())
        except Exception:
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/manifest.json":
            self._send(200, json.dumps(APP_MANIFEST, indent=2).encode("utf-8"),
                       "application/manifest+json")
            return
        if path == "/icon.svg":
            self._send(200, ICON_SVG.encode("utf-8"), "image/svg+xml")
            return
        if path == "/sw.js":
            self._send(200, SERVICE_WORKER.encode("utf-8"), "application/javascript")
            return
        if path == "/api/events":
            self._serve_events()
            return
        if path == "/ready":
            # Readiness is liveness AND a readable store. A container whose
            # volume failed to mount must not report ready.
            self._serve_ready()
            return
        if path == "/api/health":
            self._serve_health()
            return
        if path == "/api/communications" or path.startswith("/api/communications/"):
            self._serve_communications(path, parsed)
            return
        if not path.startswith("/api/self") and path not in ("/health", "/status", "/api/presence", "/api/activity",
                        "/api/relationships", "/api/capabilities", "/api/messages", "/api/work",
                        "/api/push/status", "/api/push/public-key", "/api/notify",
                        "/api/profile"):
            self._send(404, b'{"error": "not found"}', "application/json")
            return

        if self.presence_store is None:
            self._send(503, b'{"error": "presence store not initialized"}', "application/json")
            return

        view = self.presence_store.public_view()
        if path == "/status":
            body = render_dashboard(view).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path in ("/health", "/api/presence"):
            body = json.dumps(view, indent=2).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        self._serve_read_api(path, parsed)

    def _serve_ready(self) -> None:
        """Readiness: the process answers and its identity state is readable."""
        storage, identity_id = self._backend()
        if storage is None:
            self._send(503, json.dumps({"ready": False, "reason": "store not initialized"}).encode(),
                       "application/json")
            return
        try:
            namespaces = storage.list_namespaces(identity_id)
        except Exception as exc:
            self._send(503, json.dumps({
                "ready": False, "reason": f"store unreadable: {type(exc).__name__}"}).encode(),
                "application/json")
            return
        self._send(200, json.dumps({
            "ready": True,
            "identity_id": identity_id,
            "namespaces": len(namespaces),
        }).encode(), "application/json")

    def _serve_health(self) -> None:
        """Detailed subsystem health with the evidence behind each state."""
        from core.operations.health import runtime_health
        from core.operations.store import OperationsStore

        storage, identity_id = self._backend()
        if storage is None:
            self._send(503, b'{"error": "store not initialized"}', "application/json")
            return
        try:
            store = OperationsStore(storage, identity_id)
            report = runtime_health(store, presence_store=self.presence_store,
                                    storage=storage, identity_id=identity_id)
        except Exception as exc:
            # A failure to compute health is itself a health fact; report it
            # rather than returning a 500 with no explanation.
            self._send(503, json.dumps({
                "overall": "unknown",
                "error": f"{type(exc).__name__}: {exc}",
            }).encode(), "application/json")
            return
        self._send(200, json.dumps(report, indent=2).encode(), "application/json")

    def _serve_communications(self, path: str, parsed: Any) -> None:
        """Channel -> conversation -> event view, the same shape for every
        transport, plus per-message diagnostics for email."""
        from urllib.parse import parse_qs

        from core.operations.communications import channels_for, conversations_for
        from core.operations.store import OperationsStore

        storage, identity_id = self._backend()
        if storage is None:
            self._send(503, b'{"error": "store not initialized"}', "application/json")
            return
        query = parse_qs(parsed.query)
        limit = min(max(int((query.get("limit") or ["50"])[0]), 1), 500)

        try:
            store = OperationsStore(storage, identity_id)
        except Exception as exc:
            self._send(503, json.dumps({
                "error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")
            return

        # /api/communications -> channels with counts
        if path == "/api/communications":
            payload = {"identity_id": identity_id, "channels": channels_for(store)}
            self._send(200, json.dumps(payload, indent=2).encode(), "application/json")
            return

        rest = path[len("/api/communications/"):]
        parts = [p for p in rest.split("/") if p]
        if not parts:
            self._send(404, b'{"error": "channel required"}', "application/json")
            return
        channel_id = parts[0]
        try:
            conversations = conversations_for(store, channel_id, limit=limit)
        except Exception as exc:
            self._send(503, json.dumps({
                "error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")
            return
        if conversations == [] and len(parts) == 1:
            # Distinguish "unknown channel" from "known but empty".
            from core.operations.communications import get_channel

            if get_channel(channel_id) is None:
                self._send(404, json.dumps({
                    "error": f"unknown channel: {channel_id}"}).encode(), "application/json")
                return
        if len(parts) == 1:
            payload = {
                "channel": channel_id,
                "conversations": [c.to_summary() for c in conversations],
            }
            self._send(200, json.dumps(payload, indent=2).encode(), "application/json")
            return

        # /api/communications/<channel>/<conversation_id>
        target = "/".join(parts[1:])
        for conversation in conversations:
            if conversation.id == target:
                payload = conversation.to_summary()
                payload["events"] = [e.to_dict() for e in conversation.events]
                if channel_id == "email":
                    payload["diagnostics"] = self._email_diagnostics(store, conversation)
                self._send(200, json.dumps(payload, indent=2).encode(), "application/json")
                return
        self._send(404, json.dumps({"error": "conversation not found"}).encode(),
                   "application/json")

    def _email_diagnostics(self, store: Any, conversation: Any) -> list[dict[str, Any]]:
        """Per-message stage evidence for a conversation.

        This is the panel that answers "why didn't she reply?" from records:
        poll latency, generation time, SMTP acceptance, and the durable job
        status. No model claim appears here; every value is a stored timestamp
        or a stored failure reason.
        """
        from core.operations.models import _EMAIL_JOB_STAGE_FIELDS

        by_rfc: dict[str, Any] = {}
        for job in store.list_email_jobs():
            # New jobs key on rfc_message_id; jobs written before that field
            # existed used inbound_message_id. Both are RFC ids, so one lookup
            # covers legacy and current records.
            for key in (job.rfc_message_id, job.inbound_message_id):
                if key:
                    by_rfc.setdefault(key, job)
        out: list[dict[str, Any]] = []
        for event in conversation.events:
            rfc = event.rfc_message_id or ""
            job = by_rfc.get(rfc)
            if job is None:
                continue
            stages = {name: getattr(job, name, "") or None
                      for name in sorted(_EMAIL_JOB_STAGE_FIELDS)}
            out.append({
                "event_id": event.id,
                "direction": event.direction,
                "job_id": job.id,
                "status": job.status.value,
                "gmail_message_id": job.gmail_message_id or None,
                "gmail_thread_id": job.gmail_thread_id or None,
                "rfc_message_id": job.rfc_message_id or job.inbound_message_id or None,
                "failure_reason": job.failure_reason or None,
                "attempt_count": job.attempt_count,
                "worker": job.claimed_by or None,
                "stages": stages,
                "latency": job.latency_breakdown(),
                "diagnostics": job.diagnostics(),
            })
        return out

    def _serve_read_api(self, path: str, parsed: Any) -> None:
        from urllib.parse import parse_qs

        from core.operations.store import OperationsStore

        storage, identity_id = self._backend()
        if storage is None:
            self._send(503, b'{"error": "storage not initialized"}', "application/json")
            return
        if path == '/api/self' or path.startswith('/api/self/'):
            from core.self_knowledge import SelfKnowledge, SECTIONS
            section = path[len('/api/self/'): ] if path.startswith('/api/self/') else None
            if section and section not in SECTIONS:
                self._send(404, b'{"error":"unknown self-state section"}', "application/json")
                return
            payload = SelfKnowledge(storage, identity_id).snapshot([section] if section else None)
            self._send(200, json.dumps(payload).encode('utf-8'), "application/json; charset=utf-8")
            return
        store = OperationsStore(storage, identity_id)
        try:
            limit = int(parse_qs(parsed.query).get("limit", ["30"])[0])
        except (ValueError, TypeError):
            limit = 30
        limit = max(1, min(100, limit))
        try:
            if path == "/api/activity":
                from core.operations.progress import projection
                payload = {"events": _timeline(store, limit=limit), "autonomy": projection(store)}
            elif path == "/api/work":
                from core.services.integration import work_cards, specification_cards
                payload = {"jobs": work_cards(storage, identity_id), "specifications": specification_cards(storage, identity_id)}
            elif path == "/api/relationships":
                payload = {"relationships": _relationship_cards(store)}
            elif path == "/api/capabilities":
                from core.capabilities.registry import CapabilityRegistry

                payload = {"capabilities": _capability_cards(
                    CapabilityRegistry(storage), identity_id, store)}
            elif path == "/api/messages":
                payload = {"messages": _message_cards(store, limit=limit)}
            elif path == "/api/profile":
                payload = {"profile": _communication_profile()}
            elif path == "/api/push/status":
                payload = self._push_status_payload()
            elif path == "/api/push/public-key":
                payload = self._push_public_key_payload()
                if payload is None:
                    self._send(503, b'{"error": "push not configured"}', "application/json")
                    return
            elif path == "/api/notify":
                payload = self._notify_center_payload(store, limit=limit)
            else:
                self._send(404, b'{"error": "not found"}', "application/json")
                return
        except Exception as exc:
            self._send(500, json.dumps({"error": f"{type(exc).__name__}"}).encode(),
                       "application/json")
            return
        self._send(200, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _push_status_payload(self) -> dict[str, Any]:
        from core.operations.notify import NotificationManager

        manager = self._notify_manager()
        if manager is None:
            return {"available": False, "reason": "storage not initialized"}
        subs = manager.valid_subscriptions()
        last_result = ""
        last_verified = ""
        for sub in subs:
            if (sub.get("last_verified") or "") > last_verified:
                last_verified = sub.get("last_verified") or ""
            if sub.get("last_result"):
                last_result = sub.get("last_result") or last_result
        return {
            "available": bool(subs),
            "subscriptions": len(subs),
            "last_verified": last_verified,
            "last_result": last_result,
        }

    def _push_public_key_payload(self) -> Optional[dict[str, Any]]:
        manager = self._notify_manager()
        secrets = self._secret_store()
        if manager is None or secrets is None:
            return None
        try:
            public_key, _ = manager.ensure_vapid(secrets)
        except Exception:
            return None
        return {"key": public_key}

    def _notify_center_payload(self, store: Any, *, limit: int = 30) -> dict[str, Any]:
        from core.operations.notify import NotificationManager

        manager = NotificationManager(store._storage, store.identity_id)
        events = manager.list_events(limit=limit)
        attention = manager.attention_items(store)
        # Badge MUST equal attention_count (unread-only): the SSE stream and
        # the notify poll are two writers of the same UI badge, and any
        # divergence between them flickers on the phone.
        return {
            "badge": manager.attention_count(store),
            "attention": attention,
            "notifications": [e.to_dict() for e in events],
        }

    def _serve_events(self) -> None:
        """Server-Sent Events: presence, messages, notifications, badge.

        Change-triggered snapshots only (no raw provenance firehose). The
        browser reconnects automatically; existing polling remains as the
        fallback when the stream drops.
        """
        if self.presence_store is None:
            self._send(503, b'{"error": "presence store not initialized"}', "application/json")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        from core.operations.notify import NotificationManager
        from core.operations.store import OperationsStore

        storage, identity_id = self._backend()
        manager = NotificationManager(storage, identity_id)
        last_heartbeat = ""
        last_message = ""
        last_badge = -1
        last_presence_status = ""
        ticks = 0
        try:
            while True:
                try:
                    view = self.presence_store.public_view()
                    store = OperationsStore(storage, identity_id)
                    thread = store.list_messages()
                    newest = thread[-1].id if thread else ""
                    badge = manager.attention_count(store)
                    changed = (
                        view.get("last_heartbeat") != last_heartbeat
                        or newest != last_message
                        or badge != last_badge
                        or view.get("status") != last_presence_status
                        or ticks % 5 == 0
                    )
                    if changed:
                        last_heartbeat = view.get("last_heartbeat") or ""
                        last_message = newest
                        last_badge = badge
                        last_presence_status = view.get("status") or ""
                        frame = {
                            "presence": {
                                "health": view.get("health"),
                                "status": view.get("status"),
                                "activity": view.get("activity"),
                                "heartbeat_age_seconds": view.get("heartbeat_age_seconds"),
                                "service_state": view.get("service_state"),
                            },
                            "messages": {"newest": newest, "count": len(thread)},
                            "badge": badge,
                        }
                        self.wfile.write(
                            ("event: state\ndata: " + json.dumps(frame) + "\n\n").encode()
                        )
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                ticks += 1
                time.sleep(3)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _read_json_body(self) -> Optional[dict[str, Any]]:
        """Read a JSON POST body, or send the error and return None."""
        if not _post_rate_ok():
            self._send(429, b'{"error": "rate limit: slow down"}', "application/json")
            return None
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_POST_BYTES:
            self._send(400, b'{"error": "invalid request size"}', "application/json")
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, b'{"error": "invalid JSON"}', "application/json")
            return None
        if not isinstance(payload, dict):
            self._send(400, b'{"error": "invalid parameters"}', "application/json")
            return None
        return payload

    def _require_principal(self) -> bool:
        if self.presence_store is None:
            self._send(503, b'{"error": "presence store not initialized"}', "application/json")
            return False
        ok, reason = _principal_verified(self)
        if not ok:
            self._send(403, json.dumps({"error": reason}).encode("utf-8"),
                       "application/json")
            return False
        return True

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/messages":
            self._post_message()
        elif path == GMAIL_PUSH_PATH:
            self._post_gmail_push(parsed)
        elif path == "/api/push/subscribe":
            self._post_push_subscribe()
        elif path == "/api/push/unsubscribe":
            self._post_push_unsubscribe()
        elif path == "/api/notify/test":
            self._post_notify_test()
        elif path == "/api/notify/read":
            self._post_notify_read()
        elif path == "/api/notify/viewed":
            self._post_notify_viewed()
        elif path == "/api/notify/confirm":
            self._post_notify_confirm()
        else:
            self._send(404, b'{"error": "not found"}', "application/json")

    def _post_gmail_push(self, parsed: Any) -> None:
        """Accept a Pub/Sub notification for a Gmail watch.

        The endpoint's only job is to verify, durably queue, and acknowledge.
        Reading Gmail history happens on a worker so that a slow or failing
        Gmail call cannot cause Pub/Sub to redeliver into an endpoint that is
        already struggling — and, more importantly, cannot lose the
        notification. A dropped notification is an unanswered email.
        """
        # Read through self, not the module-level class, so a subclass or a
        # test harness can supply its own service.
        service = getattr(self, "gmail_push_service", None)
        if service is None:
            self._send(503, b'{"error": "gmail push not active"}', "application/json")
            return
        from urllib.parse import parse_qs

        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if not service.authorize(query):
            # 401 so Pub/Sub records an auth failure rather than treating this
            # as a transport problem, and so probing is visibly unauthorized.
            self._send(401, b'{"error": "unauthorized"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        # A Pub/Sub message is small; a large body is not a notification.
        if length <= 0 or length > 1_000_000:
            self._send(400, b'{"error": "invalid body"}', "application/json")
            return
        body = self.rfile.read(length)
        try:
            status, payload = service.handle_push(body)
        except Exception as exc:
            # Queueing failed: return 5xx so Pub/Sub retries rather than
            # dropping the notification on the floor.
            logger.exception("gmail push queueing failed")
            self._send(500, json.dumps({"error": f"{type(exc).__name__}"}).encode(),
                       "application/json")
            return
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _post_message(self) -> None:
        if not self._require_principal():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        text = payload.get("text", "")
        thread_id = payload.get("thread_id", "") if isinstance(payload, dict) else ""
        if not isinstance(text, str) or not isinstance(thread_id, str):
            self._send(400, b'{"error": "invalid parameters"}', "application/json")
            return

        from core.operations.store import OperationsStore
        from core.operations.principal import submit_principal_message

        storage, identity_id = self._backend()
        store = OperationsStore(storage, identity_id)
        try:
            message = submit_principal_message(store, text, thread_id=thread_id.strip())
        except ValueError as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode("utf-8"),
                       "application/json")
            return
        except RuntimeError as exc:
            self._send(429, json.dumps({"error": str(exc)}).encode("utf-8"),
                       "application/json")
            return
        self._send(201, json.dumps({
            "id": message.id,
            "status": message.status.value,
            "created_at": message.created_at,
        }).encode("utf-8"), "application/json; charset=utf-8")

    def _post_push_subscribe(self) -> None:
        if not self._require_principal():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        subscription = payload.get("subscription")
        device_label = payload.get("device_label", "")
        if not isinstance(subscription, dict) or not isinstance(device_label, str):
            self._send(400, b'{"error": "invalid parameters"}', "application/json")
            return
        manager = self._notify_manager()
        if manager is None:
            self._send(503, b'{"error": "storage not initialized"}', "application/json")
            return
        login = (self.headers.get("Tailscale-User-Login", "") or "").strip()
        try:
            result = manager.register_subscription(
                subscription, principal_login=login, device_label=device_label)
        except ValueError as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode("utf-8"),
                       "application/json")
            return
        self._send(201, json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")

    def _post_push_unsubscribe(self) -> None:
        if not self._require_principal():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        endpoint = payload.get("endpoint", "")
        if not isinstance(endpoint, str) or not endpoint:
            self._send(400, b'{"error": "invalid parameters"}', "application/json")
            return
        manager = self._notify_manager()
        if manager is None:
            self._send(503, b'{"error": "storage not initialized"}', "application/json")
            return
        removed = manager.remove_subscription(endpoint)
        self._send(200, json.dumps({"removed": removed}).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _post_notify_test(self) -> None:
        """Send exactly one intentional first-party TEST push.

        Single-flight: an unresolved TEST event is returned instead of
        sending another, so repeated taps never spam the phone. ntfy is NOT
        used here unless explicitly requested for fallback diagnosis.
        """
        from core.operations.notify import NotificationManager, NotifyImportance, NotifyKind

        if not self._require_principal():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        allow_fallback = bool(payload.get("fallback", False))
        manager = self._notify_manager()
        secrets = self._secret_store()
        if manager is None or secrets is None:
            self._send(503, b'{"error": "push not configured"}', "application/json")
            return
        event = manager.create_event(
            NotifyKind.TEST,
            title="Aster",
            body="First-party IdentityOS notifications are working.",
            importance=NotifyImportance.NORMAL,
            object_type="notification_center",
            deep_link="/status#/notifications",
            requires_attention=True,
            dedup_key="acceptance:test-push",
            evidence_ref="operator:acceptance-test",
        )
        if any(c.get("channel") == "webpush" and c.get("result") == "submitted"
               for c in event.channels):
            self._send(200, json.dumps({
                "id": event.id, "status": "already_submitted",
            }).encode("utf-8"), "application/json; charset=utf-8")
            return
        storage, identity_id = self._backend()
        from core.operations.store import OperationsStore

        outcome = manager.send_first_party(
            event, secret_store=secrets, badge=manager.attention_count(
                OperationsStore(storage, identity_id)))
        response: dict[str, Any] = {"id": event.id, **outcome}
        if outcome["result"] != "submitted" and allow_fallback:
            from core.capabilities.registry import CapabilityRegistry

            fallback = manager.send_ntfy_fallback(
                event, CapabilityRegistry(storage))
            response["fallback"] = fallback
        code = 200 if outcome["result"] == "submitted" else 502
        self._send(code, json.dumps(response).encode("utf-8"), "application/json; charset=utf-8")

    def _post_notify_read(self) -> None:
        if not self._require_principal():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        event_id = payload.get("id", "")
        if not isinstance(event_id, str) or not event_id:
            self._send(400, b'{"error": "invalid parameters"}', "application/json")
            return
        manager = self._notify_manager()
        if manager is None:
            self._send(503, b'{"error": "storage not initialized"}', "application/json")
            return
        manager.mark_read(event_id)
        manager.resolve(event_id, "read by principal")
        self._send(200, b'{"ok": true}', "application/json")

    def _post_notify_viewed(self) -> None:
        """Clear-on-viewed: the principal opened the notification center."""
        if not self._require_principal():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        manager = self._notify_manager()
        if manager is None:
            self._send(503, b'{"error": "storage not initialized"}', "application/json")
            return
        from core.operations.store import OperationsStore

        storage, identity_id = self._backend()
        result = manager.mark_viewed()
        result["badge"] = manager.attention_count(OperationsStore(storage, identity_id))
        self._send(200, json.dumps(result).encode("utf-8"), "application/json")

    def _post_notify_confirm(self) -> None:
        """Record the principal's own visibility confirmation (their word)."""
        if not self._require_principal():
            return
        payload = self._read_json_body()
        if payload is None:
            return
        event_id = payload.get("id", "")
        if not isinstance(event_id, str) or not event_id:
            self._send(400, b'{"error": "invalid parameters"}', "application/json")
            return
        manager = self._notify_manager()
        if manager is None:
            self._send(503, b'{"error": "storage not initialized"}', "application/json")
            return
        ok = manager.confirm_visible(event_id)
        self._send(200 if ok else 404, json.dumps({"confirmed": ok}).encode("utf-8"),
                   "application/json")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_presence_store(
    store_dir: str,
    backend_type: str,
    identity_id: str = "aster",
) -> PresenceStore:
    backend_kwargs = {}
    if backend_type == "sqlite":
        from pathlib import Path
        backend_kwargs["db_path"] = str(Path(store_dir) / "identities.db")
    else:
        backend_kwargs["root_dir"] = store_dir
    storage = get_backend(backend_type, **backend_kwargs)
    return PresenceStore(storage, identity_id)


def build_gmail_push_service(store: str, backend: str, identity: str) -> Any:
    """Construct the Gmail push service, or None when it is not configured.

    Returns None rather than raising so a deployment without OAuth credentials
    starts normally and keeps using polling. Event-driven ingest is an
    enhancement to a working system, not a precondition for it.

    This process is receive-only: it verifies, decodes, and durably queues
    notifications and keeps the watch alive. It never ingests, because the only
    configured engine lives in the operator process. The queue is shared
    storage, so the operator drains what arrives here. Two processes both
    ingesting would mean two writers racing on the same mailbox.
    """
    from core.capabilities.email.gmail_push import GmailPushConfig, GmailPushService

    config = GmailPushConfig.from_env()
    if not config.configured:
        return None
    try:
        from runtime.persistence import get_backend as _backend

        storage = _backend(backend, store)
        return GmailPushService(
            config,
            storage=storage,
            identity_id=identity,
            ingest=None,
        )
    except Exception as exc:  # pragma: no cover - configuration dependent
        logger.warning("gmail push service unavailable: %s", exc)
        return None


def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    store: str = ".identity_store",
    backend: str = "json",
    identity: str = "aster",
    gmail_push: bool = True,
) -> None:
    """Run the health endpoint server (blocks until shutdown)."""
    presence = build_presence_store(store, backend, identity)
    _HealthHandler.presence_store = presence

    server = ThreadingHTTPServer((host, port), _HealthHandler)
    push_service = build_gmail_push_service(store, backend, identity) if gmail_push else None
    _HealthHandler.gmail_push_service = push_service
    stop = threading.Event()
    renew_thread: Optional[threading.Thread] = None
    if push_service is not None:
        _HealthHandler.gmail_push_service = push_service
        # Establish (or re-establish) the watch before accepting traffic: a
        # watch that silently lapses is the exact failure this replaces.
        try:
            result = push_service.start_watch()
            if not result.get("ok"):
                logger.warning("gmail watch not established: %s", result.get("reason"))
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("gmail watch start failed: %s", exc)
        renew_thread = threading.Thread(
            target=push_service.renew_loop, args=(stop,), name="gmail-watch-renew",
            daemon=True)
        renew_thread.start()
        # No drain thread here on purpose: this process has no configured
        # engine, so it could not answer the mail it receives. The operator
        # process drains the shared queue with its real engine.

    print(f"Health server listening on http://{host}:{port}/health  (GET /health, /status)")
    if push_service is not None:
        health = push_service.health()
        print(f"Gmail push active: {health['status']} "
              f"(topic {health['config']['topic']}, history {health['history_id'] or 'none'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        stop.set()
        server.server_close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="IdentityOS presence health endpoint")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Port (default: 8787)")
    parser.add_argument("--store", default=".identity_store", help="Identity store directory")
    parser.add_argument("--backend", choices=["json", "sqlite"], default="json", help="Storage backend")
    parser.add_argument("--identity", default="aster", help="Identity ID")
    parser.add_argument("--no-gmail-push", action="store_true",
                        help="Disable event-driven Gmail ingest (polling only)")
    args = parser.parse_args(argv)

    serve(host=args.host, port=args.port, store=args.store, backend=args.backend,
          identity=args.identity, gmail_push=not args.no_gmail_push)
    return 0


if __name__ == "__main__":
    sys.exit(main())
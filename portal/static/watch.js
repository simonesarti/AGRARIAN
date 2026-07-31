/*
 * The watch page.
 *
 * Two connections, both straight from this browser to the hub and neither
 * through the portal: video from MediaMTX, alerts from ws-server. The portal is
 * asked once, for a viewer token and the URLs it opens.
 *
 * That token is flight-scoped, read-only and short-lived, which is what makes it
 * safe to hold in this script and put in a URL. The session token that bought it
 * stays in an httpOnly cookie this code cannot read — deliberately, so that a
 * bug on this page cannot leak the credential to the whole account.
 */

(function () {
  "use strict";

  const script = document.currentScript;
  const streamId = script.dataset.streamId;

  const statusEl = document.getElementById("status");
  const alertStatusEl = document.getElementById("alert-status");
  const listEl = document.getElementById("alert-list");
  const playerEl = document.getElementById("player");
  const hlsEl = document.getElementById("hls");

  const MAX_ALERTS = 50;          // the DOM, not the flight — older ones scroll off
  const RECONNECT_MS = 3000;

  let socket = null;
  let closing = false;

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = kind || "muted";
  }

  async function fetchToken() {
    const url = streamId ? `/api/viewer-token?stream_id=${encodeURIComponent(streamId)}`
                         : "/api/viewer-token";
    const resp = await fetch(url, { method: "POST" });

    if (resp.status === 401) {
      // The session went, not the flight. Nothing on this page can fix that.
      window.location.href = "/login?expired=1";
      return null;
    }

    let body = {};
    try { body = await resp.json(); } catch (e) { /* keep the status code's meaning */ }

    if (resp.status === 404) {
      setStatus("Nothing is flying on this slot right now. Start publishing and reload.", "muted");
      return null;
    }
    if (!resp.ok) {
      setStatus(body.error || `Could not start the stream (${resp.status}).`, "error");
      return null;
    }
    return body;
  }

  function startVideo(info) {
    playerEl.src = info.webrtc_url;
    hlsEl.href = info.hls_url;
    hlsEl.hidden = false;
  }

  function renderAlert(alert) {
    const li = document.createElement("li");
    li.className = "alert";

    const when = alert.datetime ? String(alert.datetime).replace("T", " ").slice(0, 19) : "";
    const head = document.createElement("div");
    head.className = "alert-head";
    // textContent throughout: the alert text originates in the processing
    // pipeline, and building this with innerHTML would make that a path into
    // the DOM.
    head.textContent = alert.alert_msg || "Alert";

    const meta = document.createElement("div");
    meta.className = "muted small";
    meta.textContent = when + (alert.frame_id !== undefined ? ` · frame ${alert.frame_id}` : "");

    li.appendChild(head);
    li.appendChild(meta);

    if (alert.image) {
      const img = document.createElement("img");
      img.alt = "Alert frame";
      img.loading = "lazy";
      img.src = "data:image/jpeg;base64," + alert.image;
      li.appendChild(img);
    }

    listEl.prepend(li);
    while (listEl.children.length > MAX_ALERTS) {
      listEl.removeChild(listEl.lastChild);
    }
  }

  function openAlerts(info) {
    socket = new WebSocket(info.ws_url);

    socket.onopen = function () {
      alertStatusEl.textContent = "Connected — alerts appear here as they happen.";
    };

    socket.onmessage = function (event) {
      let alert;
      try { alert = JSON.parse(event.data); } catch (e) { return; }
      renderAlert(alert);
    };

    socket.onclose = function () {
      if (closing) return;
      // ws-server closes the connection when the token expires and when the
      // flight ends, and those are indistinguishable from here — so retry, and
      // let the token fetch decide which it was. A landed flight 404s and stops.
      alertStatusEl.textContent = "Alert stream lost — reconnecting…";
      window.setTimeout(start, RECONNECT_MS);
    };

    socket.onerror = function () {
      alertStatusEl.textContent = "Alert stream error — reconnecting…";
    };
  }

  async function start() {
    if (socket && socket.readyState === WebSocket.OPEN) return;

    let info;
    try {
      info = await fetchToken();
    } catch (e) {
      setStatus("Could not reach the portal. Retrying…", "error");
      window.setTimeout(start, RECONNECT_MS);
      return;
    }
    if (!info) return;

    setStatus("Live — flight " + info.flight_id, "live-text");
    startVideo(info);
    openAlerts(info);
  }

  window.addEventListener("beforeunload", function () {
    closing = true;
    if (socket) socket.close();
  });

  start();
})();

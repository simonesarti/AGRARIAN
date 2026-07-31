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
  const hlsNoteEl = document.getElementById("hls-note");

  const MAX_ALERTS = 50;          // the DOM, not the flight — older ones scroll off
  const RECONNECT_MS = 3000;

  let socket = null;
  let pc = null;
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

  /*
   * WHEP, negotiated here rather than by MediaMTX's built-in reader page.
   *
   * The page was the obvious choice and it cannot be used: GET /<path>/ answers
   * 401 with WWW-Authenticate: Basic and never calls db-writer's auth hook, for
   * ?jwt=, ?token=, ?user=&pass=, Bearer and Basic alike. It is protected by
   * MediaMTX's internal user roster, which this deployment replaced with
   * authMethod: http. The WHEP endpoint underneath it has no such problem: it
   * consults the hook and accepts the viewer token in the query.
   *
   * Non-trickle: all candidates are gathered before the offer is sent, so there
   * is one request and no PATCH. It costs a moment of setup latency and removes
   * the ICE resource lifecycle entirely, which is the right trade for a page
   * that opens one stream and holds it.
   */
  const GATHER_TIMEOUT_MS = 3000;

  async function gatherComplete(pc) {
    if (pc.iceGatheringState === "complete") return;
    await new Promise(function (resolve) {
      const done = function () {
        if (pc.iceGatheringState !== "complete") return;
        pc.removeEventListener("icegatheringstatechange", done);
        resolve();
      };
      pc.addEventListener("icegatheringstatechange", done);
      // A candidate that never arrives must not hang the page. Whatever was
      // gathered by now is usually enough on a reachable network.
      window.setTimeout(resolve, GATHER_TIMEOUT_MS);
    });
  }

  /*
   * The HLS fallback exists for networks that block WebRTC's UDP, which is a
   * real case in offices. It is NOT a link to the .m3u8: Chrome, Firefox and
   * Edge have no native HLS support, so navigating to a playlist renders a blank
   * page — the symptom this replaced.
   *
   * Playing it in-page needs either native support (Safari, iOS) or Media Source
   * Extensions plus a demuxer, which is what hls.js is. Nothing is vendored
   * here, so on a browser without native support this says so rather than
   * showing black.
   */
  function nativeHls() {
    return Boolean(playerEl.canPlayType("application/vnd.apple.mpegurl"));
  }

  function startHls(info) {
    if (!nativeHls()) {
      hlsNoteEl.textContent =
        "This browser cannot play HLS without an extra library. Safari and iOS can; " +
        "Chrome, Firefox and Edge cannot. WebRTC above is the supported path here.";
      hlsNoteEl.hidden = false;
      return;
    }
    if (pc) { pc.close(); pc = null; }
    playerEl.srcObject = null;
    playerEl.src = info.hls_url;
    playerEl.play().catch(function () {
      // Autoplay refusal is not a stream failure; the controls are visible.
      hlsNoteEl.textContent = "Press play to start the HLS stream.";
      hlsNoteEl.hidden = false;
    });
    setStatus("Live over HLS — flight " + info.flight_id, "live-text");
  }

  async function startVideo(info) {
    hlsEl.hidden = false;
    hlsEl.onclick = function () { startHls(info); };

    // start() also runs when the ALERT socket reconnects, which says nothing
    // about the video. Renegotiating a healthy stream would black the picture
    // out every time the WebSocket blinked.
    if (pc && (pc.connectionState === "connected" || pc.connectionState === "connecting")) return;
    if (pc) { pc.close(); pc = null; }
    pc = new RTCPeerConnection({ iceServers: [] });

    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });

    pc.ontrack = function (event) {
      if (playerEl.srcObject !== event.streams[0]) {
        playerEl.srcObject = event.streams[0];
      }
    };

    pc.onconnectionstatechange = function () {
      if (!pc) return;
      if (pc.connectionState === "failed") {
        setStatus("Video connection failed — use the HLS link below.", "error");
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await gatherComplete(pc);

    let resp;
    try {
      resp = await fetch(info.whep_url, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: pc.localDescription.sdp,
      });
    } catch (e) {
      setStatus("Could not reach the video server. Use the HLS link below.", "error");
      return;
    }

    if (!resp.ok) {
      // 401 here is the viewer token, not the session: it is minted per flight
      // and expires on its own schedule. Reloading buys a fresh one.
      setStatus(
        resp.status === 401
          ? "The video credential expired. Reload the page."
          : `The video server refused the stream (${resp.status}).`,
        "error");
      return;
    }

    const answer = await resp.text();
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
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
    // Not awaited: the alert stream must not wait on an ICE gathering timeout,
    // and a video failure is reported in place rather than stopping the page.
    startVideo(info).catch(function () {
      setStatus("Could not start the video. Use the HLS link below.", "error");
    });
    openAlerts(info);
  }

  window.addEventListener("beforeunload", function () {
    closing = true;
    if (socket) socket.close();
    if (pc) pc.close();
  });

  start();
})();

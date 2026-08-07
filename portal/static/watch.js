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
  const videoNoteEl = document.getElementById("video-note");

  const MAX_ALERTS = 50;          // the DOM, not the flight — older ones scroll off
  const RECONNECT_MS = 3000;

  /*
   * A publisher that drops and returns inside RECONNECT_GRACE_S is the same
   * flight, on the same path, with the same viewer token (§6). Everything the
   * browser holds stays valid across it, so the page must recover on its own —
   * it used to print "Reload to try again", which made a ten-second radio glitch
   * a manual step for whoever was watching.
   *
   * Two things have to be watched, because it is not certain which one fires.
   * MediaMTX may close the reader session when the path loses its source, which
   * surfaces as a connection state change; or it may hold the session open and
   * simply stop sending, which surfaces as nothing at all and a frozen picture.
   * The state handler catches the first, the stall watchdog catches the second.
   */
  const VIDEO_RETRY_MS = 2000;
  const STALL_CHECK_MS = 1000;
  const STALL_TIMEOUT_MS = 5000;  // generous: a brief hiccup must not renegotiate

  let socket = null;
  let pc = null;
  let closing = false;

  let videoRetryTimer = null;
  let stallTimer = null;
  let lastMediaTime = 0;
  let lastProgressAt = 0;

  function note(text) {
    videoNoteEl.textContent = text;
    videoNoteEl.hidden = false;
  }

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
   * Retry the video, and only the video.
   *
   * It cannot be done by calling start(): that returns early while the alert
   * socket is open, which is exactly the state this runs in — ws-server holds
   * the viewer's socket per flight, and a publisher dropping inside the grace
   * window does not end the flight, so the socket never closes and start() would
   * do nothing.
   *
   * The token is re-fetched rather than reused, and that is what makes one
   * function serve both cases. Inside the grace window /api/viewer-token answers
   * with the same live flight and the video comes back. Once the flight has
   * really ended it answers 404, fetchToken() says so on the page and returns
   * null, and the retry stops instead of spinning against a path that is gone.
   */
  function scheduleVideoRetry(why) {
    if (closing || videoRetryTimer) return;
    setStatus(why, "muted");
    videoRetryTimer = window.setTimeout(async function () {
      videoRetryTimer = null;
      if (closing) return;

      let info;
      try {
        info = await fetchToken();
      } catch (e) {
        scheduleVideoRetry("Reconnecting to the video…");
        return;
      }
      if (!info) return;   // flight over, or the session went — fetchToken said so

      setStatus("Live — flight " + info.flight_id, "live-text");
      startVideo(info).catch(function () {
        scheduleVideoRetry("Reconnecting to the video…");
      });
    }, VIDEO_RETRY_MS);
  }

  /*
   * The picture stopped, whatever the connection says about itself.
   *
   * currentTime advancing is the only direct evidence that frames are being
   * decoded, and it is needed because connectionState lies in both directions:
   *
   *   "connected"    while no media arrives at all — MediaMTX can hold a reader
   *                  session open after its publisher goes away
   *   "disconnected" for fifteen to thirty seconds before it admits to "failed",
   *                  which is how long ICE spends on consent timeout
   *
   * The second was measured rather than guessed: a publisher dropped and brought
   * back inside the grace window recovered, but only after roughly twenty seconds,
   * because the state handler waits for "failed" and this timer used to skip any
   * state that was not "connected". Nothing acted during the gap. Watching both
   * states collapses that to STALL_TIMEOUT_MS.
   *
   * "connecting" and "new" are deliberately excluded: currentTime not advancing
   * during setup is normal, and retrying there would restart a negotiation that
   * has not finished. "failed" and "closed" belong to the state handler.
   *
   * Guarded on document.hidden because a backgrounded tab has its video
   * throttled or paused by the browser, and reconnecting then would renegotiate
   * a stream nobody is looking at.
   */
  const STALL_WATCHED_STATES = ["connected", "disconnected"];

  function watchForStall() {
    if (stallTimer) window.clearInterval(stallTimer);
    lastMediaTime = 0;
    lastProgressAt = Date.now();

    stallTimer = window.setInterval(function () {
      if (closing || document.hidden) { lastProgressAt = Date.now(); return; }
      if (!pc || STALL_WATCHED_STATES.indexOf(pc.connectionState) === -1) return;

      if (playerEl.currentTime !== lastMediaTime) {
        lastMediaTime = playerEl.currentTime;
        lastProgressAt = Date.now();
        return;
      }
      if (Date.now() - lastProgressAt > STALL_TIMEOUT_MS) {
        window.clearInterval(stallTimer);
        stallTimer = null;
        scheduleVideoRetry("Video stalled — reconnecting…");
      }
    }, STALL_CHECK_MS);
  }

  async function startVideo(info) {

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
      if (!pc || closing) return;

      // "disconnected" is often transient and ICE sometimes recovers by itself,
      // so this does not renegotiate on it — but it does not wait for "failed"
      // either, because that takes fifteen to thirty seconds of consent timeout.
      // The stall watchdog now watches this state too and acts on the picture
      // rather than on the label, so recovery costs STALL_TIMEOUT_MS whether ICE
      // admits to failing or not. "failed" and "closed" are terminal and there is
      // nothing left to wait for.
      if (pc.connectionState === "disconnected") {
        setStatus("Video interrupted — reconnecting…", "muted");
        return;
      }
      if (pc.connectionState === "failed" || pc.connectionState === "closed") {
        videoNoteEl.hidden = true;
        scheduleVideoRetry("Video interrupted — reconnecting…");
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
      scheduleVideoRetry("Could not reach the video server — retrying…");
      return;
    }

    if (!resp.ok) {
      // 401 here is the viewer token, not the session: it is minted per flight
      // and expires on its own schedule. The retry re-fetches one, so this no
      // longer asks the user to reload — and if the flight has actually ended,
      // /api/viewer-token answers 404 and the retry stops there instead.
      setStatus(
        resp.status === 401
          ? "The video credential expired — renewing…"
          : `The video server refused the stream (${resp.status}) — retrying…`,
        "muted");
      scheduleVideoRetry(statusEl.textContent);
      return;
    }

    const answer = await resp.text();
    await pc.setRemoteDescription({ type: "answer", sdp: answer });

    // Only now is there something that could stall.
    watchForStall();
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
      setStatus("Could not start the video.", "error");
    });
    openAlerts(info);
  }

  window.addEventListener("beforeunload", function () {
    closing = true;
    // closing is checked by both timers, but clearing them as well means a
    // pending retry cannot fire against a page that is already going away.
    if (videoRetryTimer) { window.clearTimeout(videoRetryTimer); videoRetryTimer = null; }
    if (stallTimer) { window.clearInterval(stallTimer); stallTimer = null; }
    if (socket) socket.close();
    if (pc) pc.close();
  });

  start();
})();

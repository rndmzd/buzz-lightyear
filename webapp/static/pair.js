(() => {
  const btnPair = document.getElementById("btn-pair");
  const btnReset = document.getElementById("btn-reset");
  const unameInput = document.getElementById("uname");
  const statusEl = document.getElementById("status");
  const qrPanel = document.getElementById("qr-panel");
  const qrImage = document.getElementById("qr-image");
  const uidLabel = document.getElementById("uid-label");
  const pairCodeWrap = document.getElementById("pair-code-wrap");
  const pairCode = document.getElementById("pair-code");
  const successPanel = document.getElementById("success-panel");
  const deviceSummary = document.getElementById("device-summary");

  let pollTimer = null;

  function setStatus(state, message) {
    statusEl.className = `status ${state || "idle"}`;
    statusEl.textContent = message || "";
  }

  function showQr(data) {
    if (data.qrcode_url) {
      qrImage.src = data.qrcode_url;
      qrPanel.classList.remove("hidden");
    } else if (data.qrcode) {
      // Raw payload only — show as text hint; most responses include a URL.
      qrPanel.classList.remove("hidden");
      qrImage.alt = "QR payload received (no image URL)";
      qrImage.removeAttribute("src");
    } else {
      qrPanel.classList.add("hidden");
    }
    uidLabel.textContent = data.uid || "—";
    if (data.pair_code) {
      pairCode.textContent = data.pair_code;
      pairCodeWrap.classList.remove("hidden");
    } else {
      pairCodeWrap.classList.add("hidden");
    }
  }

  function showSuccess(data) {
    successPanel.classList.remove("hidden");
    const summary = {
      uid: data.uid,
      device_online: data.device_online,
      toys: data.toys,
      app_status: data.app_status,
      app_online: data.app_online,
      paired_at: data.paired_at,
    };
    deviceSummary.textContent = JSON.stringify(summary, null, 2);
  }

  function applyState(data) {
    const status = data.status || "idle";
    const message =
      data.message ||
      data.error ||
      (status === "paired" ? "Paired successfully." : status);

    setStatus(status, message);

    if (data.qrcode_url || data.qrcode || data.uid) {
      showQr(data);
    }

    if (data.paired || status === "paired") {
      showSuccess(data);
      stopPolling();
      btnPair.disabled = false;
      btnPair.textContent = "Pair again";
    } else {
      successPanel.classList.add("hidden");
    }

    if (status === "error") {
      stopPolling();
      btnPair.disabled = false;
      btnPair.textContent = "Try again";
    }
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(async () => {
      try {
        const res = await fetch("/api/pairing/status");
        const data = await res.json();
        applyState(data);
      } catch (err) {
        setStatus("error", `Status check failed: ${err}`);
      }
    }, 1500);
  }

  btnPair.addEventListener("click", async () => {
    btnPair.disabled = true;
    btnPair.textContent = "Starting…";
    successPanel.classList.add("hidden");
    setStatus("connecting", "Contacting Lovense and generating QR…");

    try {
      const res = await fetch("/api/pairing/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          uname: unameInput.value.trim() || undefined,
          force_new: true,
        }),
      });
      const data = await res.json();
      if (!res.ok && data.status === "error") {
        applyState(data);
        return;
      }
      applyState(data);
      if (data.status === "waiting_for_scan" || data.status === "connecting") {
        btnPair.textContent = "Waiting for scan…";
        startPolling();
      } else if (data.paired) {
        btnPair.disabled = false;
        btnPair.textContent = "Pair again";
      } else {
        btnPair.disabled = false;
        btnPair.textContent = "Pair with Lovense";
      }
    } catch (err) {
      setStatus("error", `Request failed: ${err}`);
      btnPair.disabled = false;
      btnPair.textContent = "Pair with Lovense";
    }
  });

  btnReset.addEventListener("click", async () => {
    stopPolling();
    try {
      await fetch("/api/pairing/reset", { method: "POST" });
    } catch (_) {
      /* ignore */
    }
    qrPanel.classList.add("hidden");
    successPanel.classList.add("hidden");
    setStatus("idle", "Session cleared. Click Pair to start again.");
    btnPair.disabled = false;
    btnPair.textContent = "Pair with Lovense";
  });

  // Load any existing session state on page open.
  fetch("/api/pairing/status")
    .then((r) => r.json())
    .then((data) => {
      if (data && data.status && data.status !== "idle") {
        applyState(data);
        if (data.status === "waiting_for_scan" || data.status === "connecting") {
          startPolling();
          btnPair.disabled = true;
          btnPair.textContent = "Waiting for scan…";
        }
      }
    })
    .catch(() => {});
})();

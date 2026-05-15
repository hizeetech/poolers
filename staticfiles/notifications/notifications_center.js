(() => {
  const listEl = document.getElementById("notifications-list");
  const loadMoreBtn = document.getElementById("load-more-btn");
  const markAllBtn = document.getElementById("mark-all-read-btn");
  const enablePushBtn = document.getElementById("enable-push-btn");

  if (!listEl || !loadMoreBtn || !markAllBtn) return;

  let page = 1;
  let loading = false;

  const formatTime = (iso) => {
    try {
      const dt = new Date(iso);
      return dt.toLocaleString();
    } catch (e) {
      return iso;
    }
  };

  const renderItem = (n) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `list-group-item list-group-item-action d-flex justify-content-between align-items-start ${
      n.is_read ? "" : "fw-semibold"
    }`;
    item.dataset.id = n.id;
    item.innerHTML = `
      <div class="me-3">
        <div class="mb-1">${(n.title || "").replace(/</g, "&lt;")}</div>
        <div class="text-muted small">${(n.message || "").replace(/</g, "&lt;")}</div>
      </div>
      <div class="text-muted small" style="white-space:nowrap;">${formatTime(n.created_at)}</div>
    `;
    item.addEventListener("click", async () => {
      if (n.is_read) return;
      await markRead([n.id]);
      item.classList.remove("fw-semibold");
      n.is_read = true;
      window.dispatchEvent(new CustomEvent("notifications:updated"));
    });
    return item;
  };

  const fetchPage = async () => {
    if (loading) return;
    loading = true;
    try {
      const resp = await fetch(`/notifications/api/list/?page=${page}&page_size=20`, { credentials: "same-origin" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      (data.results || []).forEach((n) => listEl.appendChild(renderItem(n)));
      if (page >= (data.num_pages || 1)) {
        loadMoreBtn.disabled = true;
        loadMoreBtn.textContent = "No more";
      } else {
        page += 1;
      }
    } catch (e) {
      const alert = document.createElement("div");
      alert.className = "alert alert-warning mb-0";
      alert.textContent = "Failed to load notifications. Please refresh the page.";
      listEl.innerHTML = "";
      listEl.appendChild(alert);
    } finally {
      loading = false;
    }
  };

  const prependNotification = (payload) => {
    const id = payload && payload.id;
    if (!id) return;
    if (listEl.querySelector(`[data-id="${id}"]`)) return;
    const normalized = {
      ...payload,
      is_read: payload.is_read === true,
      created_at: payload.created_at || new Date().toISOString(),
    };
    listEl.insertBefore(renderItem(normalized), listEl.firstChild);
  };

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${window.location.host}/ws/notifications/`;
  let socket;

  const connect = () => {
    socket = new WebSocket(wsUrl);
    socket.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "notification") {
          prependNotification(msg.payload || {});
        }
      } catch (e) {}
    };
    socket.onclose = () => {
      setTimeout(connect, 2500);
    };
  };

  const pollLatest = async () => {
    if (loading) return;
    try {
      const resp = await fetch("/notifications/api/list/?page=1&page_size=20", { credentials: "same-origin" });
      if (!resp.ok) return;
      const data = await resp.json();
      const items = (data && data.results) ? data.results : [];
      for (const n of items.slice().reverse()) {
        prependNotification(n);
      }
    } catch (e) {}
  };

  const markRead = async (ids) => {
    await fetch("/notifications/api/mark-read/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      credentials: "same-origin",
      body: JSON.stringify({ ids }),
    });
  };

  const markAllRead = async () => {
    await fetch("/notifications/api/mark-all-read/", {
      method: "POST",
      headers: { "X-CSRFToken": getCsrfToken() },
      credentials: "same-origin",
    });
    Array.from(listEl.querySelectorAll(".list-group-item.fw-semibold")).forEach((el) => el.classList.remove("fw-semibold"));
    window.dispatchEvent(new CustomEvent("notifications:updated"));
  };

  const getCsrfToken = () => {
    const name = "csrftoken=";
    const parts = document.cookie.split(";").map((p) => p.trim());
    for (const p of parts) {
      if (p.startsWith(name)) return decodeURIComponent(p.substring(name.length));
    }
    return "";
  };

  loadMoreBtn.addEventListener("click", fetchPage);
  markAllBtn.addEventListener("click", markAllRead);
  if (enablePushBtn) {
    enablePushBtn.addEventListener("click", async () => {
      enablePushBtn.disabled = true;
      try {
        const ok = await (window.enablePushNotifications ? window.enablePushNotifications() : false);
        enablePushBtn.textContent = ok ? "Push Enabled" : "Enable Push";
      } finally {
        enablePushBtn.disabled = false;
      }
    });
  }

  fetchPage();
  connect();
  setInterval(pollLatest, 15000);
})();

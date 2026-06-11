(() => {
  const badge = document.getElementById("notificationsBadge");

  const setCount = (count) => {
    if (!badge) return;
    const n = parseInt(count || 0, 10) || 0;
    badge.textContent = `${n}`;
    if (n > 0) badge.classList.remove("d-none");
    else badge.classList.add("d-none");
  };

  const getCount = () => {
    if (!badge) return 0;
    return parseInt(badge.textContent || "0", 10) || 0;
  };

  const popupQueue = [];
  let popupActive = false;
  const shownPopupKeys = new Set();

  const escapeHtml = (value) => String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

  const buildPopupConfig = (payload, sourceType) => {
    const data = (payload && payload.data) || {};
    const popupCategory = (data.popup_category || "").toString().trim().toLowerCase();
    const deliveryChannel = (data.delivery_channel || (sourceType === "broadcast" ? "broadcast" : "")).toString().trim().toLowerCase();

    if (sourceType !== "broadcast" && popupCategory !== "message" && !["in_app", "email", "broadcast"].includes(deliveryChannel)) {
      return null;
    }

    let text = "You have a new message. Please check the notification bell.";
    if (deliveryChannel === "email") {
      text = "You have a new message. Please check your email for the message.";
    } else if (deliveryChannel === "broadcast") {
      text = "You have a new broadcast message. Please check the notification bell.";
    }

    const preview = (payload && payload.message ? String(payload.message) : "").trim();
    return {
      icon: "info",
      title: (payload && payload.title) ? String(payload.title) : "New Message",
      text,
      html: preview
        ? `<div class="text-start"><div>${escapeHtml(text)}</div><div class="text-muted small mt-2">${escapeHtml(preview.slice(0, 180))}</div></div>`
        : undefined,
      showCloseButton: true,
      confirmButtonText: "Close",
      confirmButtonColor: "#0d6efd",
    };
  };

  const flushPopupQueue = () => {
    if (popupActive || !popupQueue.length || typeof Swal === "undefined") return;
    popupActive = true;
    const config = popupQueue.shift();
    Swal.fire(config).finally(() => {
      popupActive = false;
      flushPopupQueue();
    });
  };

  const enqueuePopup = (payload, sourceType) => {
    const popupConfig = buildPopupConfig(payload, sourceType);
    if (!popupConfig) return;
    const popupKey = `${sourceType}:${payload && payload.id ? payload.id : ""}:${popupConfig.title}:${popupConfig.text}`;
    if (shownPopupKeys.has(popupKey)) return;
    shownPopupKeys.add(popupKey);
    popupQueue.push(popupConfig);
    flushPopupQueue();
  };

  const prependSystemNotification = (payload, options = {}) => {
    const systemTab = document.getElementById("system-tab");
    if (!systemTab) return;

    const emptyState = systemTab.querySelector(".notification-item.text-center");
    if (emptyState) emptyState.remove();

    const link = document.createElement("a");
    link.href = "/notifications/";
    link.className = "text-decoration-none";

    const item = document.createElement("div");
    item.className = `notification-item ${options.unread === false ? "" : "unread"}`.trim();

    const content = document.createElement("div");
    content.className = "notification-content";

    const title = document.createElement("div");
    title.className = "notification-title";
    title.textContent = (payload.title || "Notification").toString();

    const text = document.createElement("div");
    text.className = "notification-text";
    text.textContent = (payload.message || "").toString().slice(0, 140);

    const meta = document.createElement("div");
    meta.className = "text-muted small mt-1";
    meta.style.fontSize = "0.7rem";
    meta.textContent = options.metaText || "just now";

    content.appendChild(title);
    if (text.textContent) content.appendChild(text);
    content.appendChild(meta);
    item.appendChild(content);
    link.appendChild(item);
    systemTab.insertBefore(link, systemTab.firstChild);

    const items = systemTab.querySelectorAll("a .notification-item");
    if (items.length > 10) {
      const last = items[items.length - 1];
      const anchor = last.closest("a");
      if (anchor) anchor.remove();
    }
  };

  const refreshCount = async () => {
    try {
      const resp = await fetch("/notifications/api/unread-count/", { credentials: "same-origin" });
      const data = await resp.json();
      const count = (data && typeof data.unread_count === "number") ? data.unread_count : null;
      if (count === null) return;
      setCount(count);
    } catch (e) {}
  };

  window.addEventListener("notifications:updated", () => {
    const current = getCount();
    if (current > 0) setCount(current - 1);
  });

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${window.location.host}/ws/notifications/`;
  let socket;

  const getCsrfToken = () => {
    const name = "csrftoken=";
    const parts = document.cookie.split(";").map((p) => p.trim());
    for (const p of parts) {
      if (p.startsWith(name)) return decodeURIComponent(p.substring(name.length));
    }
    return "";
  };

  const urlBase64ToUint8Array = (base64String) => {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
    return outputArray;
  };

  const subscribePush = async () => {
    const vapidPublicKey = (window.__VAPID_PUBLIC_KEY__ || "").trim();
    if (!vapidPublicKey) return false;
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
    if (Notification.permission !== "granted") return false;

    const reg = await navigator.serviceWorker.register("/sw.js");
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
      });
    }

    await fetch("/notifications/api/webpush/subscribe/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
      credentials: "same-origin",
      body: JSON.stringify(sub),
    });
    return true;
  };

  window.enablePushNotifications = async () => {
    if (!("Notification" in window)) return false;
    if (Notification.permission === "denied") return false;
    if (Notification.permission === "default") {
      const res = await Notification.requestPermission();
      if (res !== "granted") return false;
    }
    return subscribePush();
  };

  const connect = () => {
    socket = new WebSocket(wsUrl);

    socket.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "notification") {
          const current = getCount();
          setCount(current + 1);
          const payload = msg.payload || {};
          prependSystemNotification(payload);
          enqueuePopup(payload, "notification");
        } else if (msg.type === "broadcast") {
          const payload = msg.payload || {};
          setCount(getCount() + 1);
          prependSystemNotification(payload);
          enqueuePopup(payload, "broadcast");
        }
      } catch (e) {}
    };

    socket.onclose = () => {
      setTimeout(connect, 2500);
    };
  };

  setCount(parseInt(badge.textContent || "0", 10) || 0);
  connect();
  refreshCount();
  setInterval(refreshCount, 15000);
  subscribePush().catch(() => {});
})();

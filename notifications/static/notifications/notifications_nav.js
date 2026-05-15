(() => {
  const badge = document.getElementById("notificationsBadge");
  if (!badge) return;

  const setCount = (count) => {
    const n = parseInt(count || 0, 10) || 0;
    badge.textContent = `${n}`;
    if (n > 0) badge.classList.remove("d-none");
    else badge.classList.add("d-none");
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
    const current = parseInt(badge.textContent || "0", 10) || 0;
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
          const current = parseInt(badge.textContent || "0", 10) || 0;
          setCount(current + 1);
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

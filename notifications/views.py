import json

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from .models import Notification, WebPushSubscription


@login_required
def notification_center(request):
    return render(request, "notifications/center.html")


@login_required
def api_notifications_list(request):
    page = int(request.GET.get("page", "1") or 1)
    page_size = min(int(request.GET.get("page_size", "20") or 20), 100)
    qs = Notification.objects.filter(recipient=request.user).order_by("-created_at")

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page)
    items = [
        {
            "id": n.id,
            "notification_type": n.notification_type,
            "title": n.title,
            "message": n.message,
            "data": n.data,
            "is_read": bool(n.is_read),
            "created_at": n.created_at.isoformat(),
        }
        for n in page_obj.object_list
    ]
    return JsonResponse({"results": items, "page": page_obj.number, "num_pages": paginator.num_pages, "count": paginator.count})


@login_required
def api_unread_count(request):
    unread = Notification.objects.filter(recipient=request.user, is_read=False).count()
    return JsonResponse({"unread_count": unread})


@login_required
@require_POST
def api_notifications_mark_read(request):
    payload = json.loads(request.body.decode("utf-8") or "{}")
    ids = payload.get("ids") or []
    now = timezone.now()
    Notification.objects.filter(recipient=request.user, id__in=ids, is_read=False).update(is_read=True, read_at=now)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def api_notifications_mark_all_read(request):
    now = timezone.now()
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True, read_at=now)
    return JsonResponse({"ok": True})


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def api_webpush_subscribe(request):
    payload = json.loads(request.body.decode("utf-8") or "{}")
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not endpoint or not p256dh or not auth:
        return JsonResponse({"ok": False, "error": "Invalid subscription payload."}, status=400)

    WebPushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"user": request.user, "p256dh": p256dh, "auth": auth, "user_agent": request.META.get("HTTP_USER_AGENT", "")},
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def api_webpush_unsubscribe(request):
    payload = json.loads(request.body.decode("utf-8") or "{}")
    endpoint = payload.get("endpoint")
    if not endpoint:
        return JsonResponse({"ok": False, "error": "Missing endpoint."}, status=400)
    WebPushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({"ok": True})


def service_worker(request):
    js = """
self.addEventListener('push', function(event) {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  const title = (data && data.title) ? data.title : 'Notification';
  const body = (data && data.body) ? data.body : '';
  const url = (data && data.data && data.data.url) ? data.data.url : '/notifications/';
  const options = {
    body: body,
    data: { url: url, payload: data }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const url = (event.notification && event.notification.data && event.notification.data.url) ? event.notification.data.url : '/notifications/';
  event.waitUntil(clients.openWindow(url));
});
"""
    return HttpResponse(js, content_type="application/javascript")

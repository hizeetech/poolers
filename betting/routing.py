from django.urls import re_path

from .consumers import AdminBetTicketConsumer


websocket_urlpatterns = [
    re_path(r"ws/admin/betticket/$", AdminBetTicketConsumer.as_asgi()),
]

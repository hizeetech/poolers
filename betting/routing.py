from django.urls import re_path

from .consumers import AdminBetTicketConsumer, AdminUserWithdrawalConsumer


websocket_urlpatterns = [
    re_path(r"ws/admin/betticket/$", AdminBetTicketConsumer.as_asgi()),
    re_path(r"ws/admin/userwithdrawal/$", AdminUserWithdrawalConsumer.as_asgi()),
]

from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/uip/dashboard/$', consumers.DashboardConsumer.as_asgi()),
]

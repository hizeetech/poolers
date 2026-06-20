import json

from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.serializers.json import DjangoJSONEncoder


class NotificationsConsumer(AsyncWebsocketConsumer):
    async def _send_event(self, event_type, payload):
        await self.send(
            text_data=json.dumps(
                {"type": event_type, "payload": payload},
                cls=DjangoJSONEncoder,
            )
        )

    async def connect(self):
        user = self.scope.get("user")
        if not user or user.is_anonymous:
            await self.close()
            return

        self.user_group = f"notifications_user_{user.id}"
        await self.channel_layer.group_add(self.user_group, self.channel_name)
        await self.channel_layer.group_add("notifications_broadcast", self.channel_name)
        if getattr(user, "user_type", None) == "finance" or getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == "admin":
            await self.channel_layer.group_add("finance_broadcast", self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "user_group"):
            await self.channel_layer.group_discard(self.user_group, self.channel_name)
        await self.channel_layer.group_discard("notifications_broadcast", self.channel_name)
        await self.channel_layer.group_discard("finance_broadcast", self.channel_name)

    async def notifications_push(self, event):
        await self._send_event("notification", event.get("payload", {}))

    async def notifications_broadcast(self, event):
        await self._send_event("broadcast", event.get("payload", {}))

    async def retail_event(self, event):
        await self._send_event("retail_event", event.get("payload", {}))

    async def finance_event(self, event):
        await self._send_event("finance_event", event.get("payload", {}))

    async def wallet_event(self, event):
        await self._send_event("wallet_event", event.get("payload", {}))

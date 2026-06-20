import json

from channels.generic.websocket import AsyncWebsocketConsumer


class AdminBetTicketConsumer(AsyncWebsocketConsumer):
    group_name = "admin_betticket_changelist"

    async def connect(self):
        user = self.scope.get("user")
        if not user or user.is_anonymous or not getattr(user, "is_staff", False):
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def admin_betticket_refresh(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "admin_betticket_refresh",
                    "payload": event.get("payload", {}),
                }
            )
        )


class AdminUserWithdrawalConsumer(AsyncWebsocketConsumer):
    group_name = "admin_userwithdrawal_changelist"

    async def connect(self):
        user = self.scope.get("user")
        if not user or user.is_anonymous or not getattr(user, "is_staff", False):
            await self.close()
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def admin_userwithdrawal_refresh(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "admin_userwithdrawal_refresh",
                    "payload": event.get("payload", {}),
                }
            )
        )

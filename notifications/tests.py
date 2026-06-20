import json
from uuid import uuid4
from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from notifications.consumers import NotificationsConsumer


class NotificationsConsumerTests(SimpleTestCase):
    def test_wallet_event_serializes_uuid_payload(self):
        consumer = NotificationsConsumer()
        consumer.send = AsyncMock()

        payload_id = uuid4()

        async_to_sync(consumer.wallet_event)(
            {
                "payload": {
                    "recent_transactions": [
                        {
                            "id": payload_id,
                            "description": "Wallet refund",
                        }
                    ]
                }
            }
        )

        consumer.send.assert_awaited_once()
        message = consumer.send.await_args.kwargs["text_data"]
        decoded = json.loads(message)

        self.assertEqual(decoded["type"], "wallet_event")
        self.assertEqual(
            decoded["payload"]["recent_transactions"][0]["id"],
            str(payload_id),
        )

import json
import logging
import os

import pika

logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
EXCHANGE = "edura.events"


def publish_event(routing_key: str, payload: dict) -> bool:
    """Publish to the shared topic exchange (notification_service sends the email). True on success."""
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
        channel = connection.channel()
        channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
        channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=routing_key,
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )
        connection.close()
        logger.info("Published %s for user_id=%s", routing_key, payload.get("user_id"))
        return True
    except Exception as exc:
        logger.error("Failed to publish %s: %s", routing_key, exc)
        return False

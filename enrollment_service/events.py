import json
import logging
import os
from datetime import datetime

import pika

logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
EXCHANGE_NAME = "edura.events"
ROUTING_KEY_ACTIVATED = "enrollment.activated"


def publish_enrollment_activated(
    enrollment_id: int, student_id: int, course_id: int, expires_at: datetime | None
):
    """
    Publishes enrollment.activated event to edura.events exchange.
    """
    payload = {
        "enrollment_id": enrollment_id,
        "student_id": student_id,
        "course_id": course_id,
        "activated_at": datetime.utcnow().isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    try:
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=RABBITMQ_HOST, connection_attempts=3, retry_delay=2
            )
        )
        channel = connection.channel()
        channel.exchange_declare(
            exchange=EXCHANGE_NAME, exchange_type="topic", durable=True
        )
        channel.basic_publish(
            exchange=EXCHANGE_NAME,
            routing_key=ROUTING_KEY_ACTIVATED,
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        connection.close()
        logger.info(
            f"Published enrollment.activated for enrollment {enrollment_id} (student: {student_id}, course: {course_id})"
        )
    except Exception as e:
        logger.error(
            f"Failed to publish enrollment.activated for enrollment {enrollment_id}: {e}"
        )

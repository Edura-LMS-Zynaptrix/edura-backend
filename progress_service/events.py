import json
import logging
import os
from datetime import datetime

import pika

logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
EXCHANGE_NAME = "edura.events"
ROUTING_KEY_CERTIFICATE_ISSUED = "certificate.issued"


def publish_certificate_issued(
    student_id: int, course_id: int, certificate_id: int, issued_at: datetime = None
):
    """
    Publishes certificate.issued event to edura.events exchange when certificate eligibility is met.
    """
    payload = {
        "student_id": student_id,
        "course_id": course_id,
        "certificate_id": certificate_id,
        "issued_at": (
            issued_at.isoformat() if issued_at else datetime.utcnow().isoformat()
        ),
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
            routing_key=ROUTING_KEY_CERTIFICATE_ISSUED,
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        connection.close()
        logger.info(
            f"Published certificate.issued for student {student_id}, course {course_id}, cert {certificate_id}"
        )
    except Exception as e:
        logger.error(
            f"Failed to publish certificate.issued for student {student_id}, course {course_id}: {e}"
        )

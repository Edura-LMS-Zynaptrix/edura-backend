import json
import logging
import os

import pika

logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
EXCHANGE = "edura.events"


def publish_course_published(course_id: int, teacher_id: int, title: str) -> None:
    payload = json.dumps({"course_id": course_id, "teacher_id": teacher_id, "title": title})
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
        channel = connection.channel()
        channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
        channel.basic_publish(
            exchange=EXCHANGE,
            routing_key="course.published",
            body=payload,
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )
        connection.close()
        logger.info("Published course.published event for course_id=%s", course_id)
    except Exception as exc:
        logger.error("Failed to publish course.published event: %s", exc)

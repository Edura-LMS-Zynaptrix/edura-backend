import json
import logging
import os

import pika

logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
EXCHANGE_NAME = "edura.events"
ROUTING_KEY_SUCCESS = "payment.success"


def publish_payment_success(
    student_id: int, course_id: int, order_id: str, amount: float
):
    """
    Publishes payment.success event to edura.events exchange.
    """
    payload = {
        "student_id": student_id,
        "course_id": course_id,
        "order_id": order_id,
        "amount": amount,
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
            routing_key=ROUTING_KEY_SUCCESS,
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        connection.close()
        logger.info(f"Published payment.success for order {order_id}")
    except Exception as e:
        logger.error(
            f"Failed to publish payment.success event for order {order_id}: {e}"
        )

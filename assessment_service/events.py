import json
import logging
import os

import pika

logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
EXCHANGE_NAME = "edura.events"
ROUTING_KEY_GRADED = "assessment.graded"


def publish_assessment_graded(
    student_id: int, assessment_id: int, score: float, passed: bool
):
    """
    Publishes assessment.graded event to edura.events exchange.
    """
    payload = {
        "student_id": student_id,
        "assessment_id": assessment_id,
        "score": score,
        "passed": passed,
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
            routing_key=ROUTING_KEY_GRADED,
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        connection.close()
        logger.info(
            f"Published assessment.graded for student {student_id}, assessment {assessment_id}"
        )
    except Exception as e:
        logger.error(
            f"Failed to publish assessment.graded for student {student_id}, assessment {assessment_id}: {e}"
        )

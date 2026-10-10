import json
import logging
import os
import time

import pika
from database import SessionLocal
from referrals import credit_on_enrollment

logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
EXCHANGE_NAME = "edura.events"
QUEUE_NAME = "user.referrals.queue"


def _handle(ch, method, properties, body):
    db = SessionLocal()
    try:
        student_id = json.loads(body).get("student_id")
        if student_id and credit_on_enrollment(db, int(student_id)):
            logger.info("Credited referral for first enrollment of user %s", student_id)
    except Exception as e:
        db.rollback()
        logger.error("Referral credit failed: %s", e)
    finally:
        db.close()
        # Acked either way: crediting is idempotent, and a poison message
        # must not be redelivered in a loop.
        ch.basic_ack(delivery_tag=method.delivery_tag)


def start_referral_consumer() -> None:
    while True:
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=RABBITMQ_HOST, connection_attempts=10, retry_delay=3)
            )
            channel = connection.channel()
            channel.exchange_declare(exchange=EXCHANGE_NAME, exchange_type="topic", durable=True)
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            channel.queue_bind(exchange=EXCHANGE_NAME, queue=QUEUE_NAME, routing_key="enrollment.activated")
            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=_handle)
            logger.info("Referral consumer listening on %s", QUEUE_NAME)
            channel.start_consuming()
        except Exception as e:
            logger.warning("Referral consumer lost RabbitMQ (%s): %s; retrying in 5s", RABBITMQ_HOST, e)
            time.sleep(5)

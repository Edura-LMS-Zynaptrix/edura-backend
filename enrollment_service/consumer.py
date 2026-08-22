import json
import logging
import os

import pika
from database import SessionLocal
from events import publish_enrollment_activated
from models import Enrollment, EnrollmentStatus
from services import activate_or_extend_enrollment, suspend_enrollment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
EXCHANGE_NAME = "edura.events"
QUEUE_NAME = "enrollment.queue"


def process_message(ch, method, properties, body):
    routing_key = method.routing_key
    logger.info(f"Received message with routing key: {routing_key}")
    try:
        data = json.loads(body)
        db = SessionLocal()
        try:
            if routing_key == "payment.success":
                student_id = data.get("student_id")
                course_id = data.get("course_id")
                if student_id and course_id:
                    enrollment = activate_or_extend_enrollment(db, student_id, course_id)
                    logger.info(f"Activated/Extended enrollment for student {student_id}, course {course_id}")
                    publish_enrollment_activated(
                        enrollment_id=enrollment.id,
                        student_id=enrollment.student_id,
                        course_id=enrollment.course_id,
                        expires_at=enrollment.expires_at,
                    )
            elif routing_key == "subscription.expired":
                student_id = data.get("student_id")
                course_id = data.get("course_id")
                if student_id and course_id:
                    suspend_enrollment(db, student_id, course_id)
                    logger.info(f"Suspended enrollment for student {student_id}, course {course_id}")
            ch.basic_ack(delivery_tag=method.delivery_tag)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error processing message ({routing_key}): {e}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def start_consumer():
    logger.info(f"Connecting to RabbitMQ host: {RABBITMQ_HOST}...")
    while True:
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=RABBITMQ_HOST, connection_attempts=10, retry_delay=3
                )
            )
            channel = connection.channel()
            channel.exchange_declare(exchange=EXCHANGE_NAME, exchange_type="topic", durable=True)
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            channel.queue_bind(exchange=EXCHANGE_NAME, queue=QUEUE_NAME, routing_key="payment.success")
            channel.queue_bind(exchange=EXCHANGE_NAME, queue=QUEUE_NAME, routing_key="subscription.expired")

            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=process_message)
            logger.info("Enrollment consumer started successfully. Listening on enrollment.queue...")
            channel.start_consuming()
            break
        except Exception as e:
            logger.warning(f"Consumer failed to connect to RabbitMQ ({RABBITMQ_HOST}): {e}. Retrying in 5s...")
            import time
            time.sleep(5)



if __name__ == "__main__":
    start_consumer()


import json
import logging
import os
import time

import pika
from database import SessionLocal
from services import process_assessment_graded, process_lesson_watched

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
EXCHANGE_NAME = "edura.events"
QUEUE_NAME = "progress.queue"


def process_message(ch, method, properties, body):
    routing_key = method.routing_key
    logger.info(f"Received message with routing key: {routing_key}")
    try:
        data = json.loads(body)
        db = SessionLocal()
        try:
            if routing_key == "lesson.watched":
                student_id = data.get("student_id")
                course_id = data.get("course_id")
                lesson_id = data.get("lesson_id")
                total_lessons = data.get("total_lessons")
                watch_duration = data.get("watch_duration", 0)
                last_position = data.get("last_position", 0)

                if student_id and course_id and lesson_id:
                    process_lesson_watched(
                        db,
                        student_id=student_id,
                        course_id=course_id,
                        lesson_id=lesson_id,
                        total_lessons=total_lessons,
                        watch_duration=watch_duration,
                        last_position=last_position,
                    )
                    logger.info(
                        f"Processed lesson.watched for student {student_id}, lesson {lesson_id}, course {course_id}"
                    )
            elif routing_key == "assessment.graded":
                student_id = data.get("student_id")
                course_id = data.get("course_id", 1)  # Default to 1 if unspecified
                assessment_id = data.get("assessment_id")
                score = float(data.get("score", 0))
                passed = bool(data.get("passed", False))

                if student_id and assessment_id:
                    process_assessment_graded(
                        db,
                        student_id=student_id,
                        course_id=course_id,
                        assessment_id=assessment_id,
                        score=score,
                        passed=passed,
                    )
                    logger.info(
                        f"Processed assessment.graded for student {student_id}, assessment {assessment_id}"
                    )

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
            channel.exchange_declare(
                exchange=EXCHANGE_NAME, exchange_type="topic", durable=True
            )
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            channel.queue_bind(
                exchange=EXCHANGE_NAME,
                queue=QUEUE_NAME,
                routing_key="lesson.watched",
            )
            channel.queue_bind(
                exchange=EXCHANGE_NAME,
                queue=QUEUE_NAME,
                routing_key="assessment.graded",
            )

            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=process_message)
            logger.info(
                "Progress consumer started successfully. Listening on progress.queue..."
            )
            channel.start_consuming()
            break
        except Exception as e:
            logger.warning(
                f"Consumer failed to connect to RabbitMQ ({RABBITMQ_HOST}): {e}. Retrying in 5s..."
            )
            time.sleep(5)


if __name__ == "__main__":
    start_consumer()

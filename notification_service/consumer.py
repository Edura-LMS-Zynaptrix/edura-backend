import json
import logging
import os
import time
from datetime import datetime, timezone

import pika
from database import SessionLocal
from email_service import render_email, send_email
from models import (
    NotificationChannel,
    NotificationEvent,
    NotificationLog,
    NotificationStatus,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
EXCHANGE_NAME = "edura.events"
QUEUE_NAME = "notification.queue"


def process_message(ch, method, properties, body):
    routing_key = method.routing_key
    logger.info(f"Received event with routing key: {routing_key}")
    db = SessionLocal()

    try:
        data = json.loads(body)
        email = data.get("email") or data.get("student_email")
        recipient_id = data.get("student_id") or data.get("user_id") or 0

        if not email:
            logger.warning(f"No email address found in event payload: {data}")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            db.close()
            return

        subject = ""
        template_name = ""
        context = {}
        event_type = NotificationEvent.general
        reference_id = None

        if routing_key == "auth.otp-requested":
            subject = "Your Edura Verification Code"
            template_name = "otp_email.html"
            event_type = NotificationEvent.otp_requested
            context = {
                "otp_code": data.get("otp_code", "000000"),
                "expires_in_minutes": data.get("expires_in_minutes", 5),
            }
        elif routing_key == "payment.success":
            subject = "Payment Confirmation - Edura"
            template_name = "payment_confirm.html"
            event_type = NotificationEvent.payment_received
            context = {
                "order_id": data.get("order_id", "N/A"),
                "course_title": data.get("course_title", "Course"),
                "amount": data.get("amount", 0.0),
            }
        elif routing_key == "certificate.issued":
            subject = "Certificate of Completion Issued!"
            template_name = "cert_issued.html"
            event_type = NotificationEvent.certificate_issued
            reference_id = data.get("certificate_id")
            context = {
                "student_name": data.get("student_name", "Student"),
                "course_title": data.get("course_title", "Course"),
                "issued_at": data.get("issued_at", "recently"),
                "download_url": data.get("download_url", "#"),
            }
        elif routing_key in ("proctoring.violation", "violation.alert"):
            subject = "Academic Integrity Alert"
            template_name = "violation_alert.html"
            event_type = NotificationEvent.proctoring_violation
            context = {
                "violation_details": data.get(
                    "violation_details", "Suspicious activity detected."
                )
            }
        else:
            logger.info(f"Unhandled routing key: {routing_key}")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            db.close()
            return

        # Render email content
        html_body = render_email(template_name, context)

        # Send Email via SMTP
        try:
            send_email(to_email=email, subject=subject, html_body=html_body)

            log_entry = NotificationLog(
                recipient_id=recipient_id,
                recipient_email=email,
                event_type=event_type,
                channel=NotificationChannel.email,
                status=NotificationStatus.sent,
                title=subject,
                body=html_body,
                reference_id=reference_id,
                sent_at=datetime.now(timezone.utc),
            )
            db.add(log_entry)
            db.commit()

            ch.basic_ack(delivery_tag=method.delivery_tag)
            logger.info(f"Successfully processed {routing_key} for {email}")

        except Exception as smtp_err:
            logger.error(f"SMTP delivery failed for {routing_key} to {email}: {smtp_err}")
            db.rollback()

            # Record failure in NotificationLog
            log_entry = NotificationLog(
                recipient_id=recipient_id,
                recipient_email=email,
                event_type=event_type,
                channel=NotificationChannel.email,
                status=NotificationStatus.failed,
                title=subject,
                body=html_body,
                reference_id=reference_id,
                error_message=str(smtp_err),
            )
            db.add(log_entry)
            db.commit()

            # Requeue message for retry
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    except Exception as e:
        logger.error(f"Error processing message ({routing_key}): {e}")
        db.rollback()
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    finally:
        db.close()


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

            binding_keys = [
                "auth.otp-requested",
                "payment.success",
                "certificate.issued",
                "proctoring.violation",
                "violation.alert",
            ]
            for rk in binding_keys:
                channel.queue_bind(
                    exchange=EXCHANGE_NAME, queue=QUEUE_NAME, routing_key=rk
                )

            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=process_message)
            logger.info(
                "Notification consumer started successfully. Listening on notification.queue..."
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

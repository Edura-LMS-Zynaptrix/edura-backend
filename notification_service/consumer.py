import base64
import json
import logging
import os
import time
from datetime import datetime, timezone

import pika
from database import Base, SessionLocal, engine
from email_service import render_email, send_email
from models import (
    NotificationChannel,
    NotificationEvent,
    NotificationLog,
    NotificationStatus,
)
from sqlalchemy import text

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
EXCHANGE_NAME = "edura.events"
QUEUE_NAME = "notification.queue"


def process_message(  # noqa: C901 - one branch per event type
    ch, method, properties, body
):
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
        attachments = []

        if routing_key == "auth.otp-requested":
            subject = "Your Edura Verification Code"
            template_name = "otp_email.html"
            event_type = NotificationEvent.otp_requested
            context = {
                "otp_code": data.get("otp_code", "000000"),
                "expires_in_minutes": data.get("expires_in_minutes", 5),
            }
        elif routing_key == "auth.email-verification-requested":
            subject = "Verify your Edura email address"
            template_name = "email_verification.html"
            event_type = NotificationEvent.email_verification_requested
            minutes = int(data.get("expires_in_minutes", 1440))
            context = {
                "verify_url": data.get("verify_url", "#"),
                "expires_in_hours": max(1, round(minutes / 60)),
            }
        elif routing_key == "auth.password-reset-requested":
            subject = "Reset your Edura password"
            template_name = "password_reset.html"
            event_type = NotificationEvent.password_reset_requested
            context = {
                "reset_url": data.get("reset_url", "#"),
                "expires_in_minutes": data.get("expires_in_minutes", 60),
            }
        elif routing_key == "auth.password-changed":
            subject = "Your Edura password was changed"
            template_name = "password_changed.html"
            event_type = NotificationEvent.password_changed
            context = {}
        elif routing_key == "report.progress":
            month = data.get("month", "")
            subject = f"Your Edura Progress Report - {month}".rstrip(" -")
            template_name = "progress_report.html"
            event_type = NotificationEvent.progress_report
            context = {
                "student_name": data.get("student_name", "Student"),
                "month": month,
            }
            attachments = [
                {
                    "filename": data.get("filename", "Progress_Report.pdf"),
                    "data": base64.b64decode(data["pdf_base64"]),
                    "subtype": "pdf",
                }
            ]
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
            send_email(
                to_email=email,
                subject=subject,
                html_body=html_body,
                attachments=attachments,
            )

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
            logger.error(
                f"SMTP delivery failed for {routing_key} to {email}: {smtp_err}"
            )
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

            # The failure is recorded above. Requeueing would redeliver it
            # immediately and forever while SMTP is down or unconfigured.
            ch.basic_ack(delivery_tag=method.delivery_tag)

    except Exception as e:
        logger.error(f"Error processing message ({routing_key}): {e}")
        db.rollback()
        # Don't requeue: a message that fails here fails the same way every
        # time, and requeueing would redeliver it in a tight loop.
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
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
                "auth.email-verification-requested",
                "auth.password-reset-requested",
                "auth.password-changed",
                "report.progress",
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


def prepare_database():
    """The container runs this consumer, not the API app, so its tables are created here."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Notification tables verified.")
    except Exception as e:
        logger.error(f"Error creating notification tables: {e}")

    # create_all() never alters an existing Postgres enum type.
    statements = (
        "ALTER TYPE notificationevent ADD VALUE IF NOT EXISTS 'password_reset_requested'",
        "ALTER TYPE notificationevent ADD VALUE IF NOT EXISTS 'password_changed'",
        "ALTER TYPE notificationevent ADD VALUE IF NOT EXISTS 'email_verification_requested'",
        "ALTER TYPE notificationevent ADD VALUE IF NOT EXISTS 'progress_report'",
    )
    try:
        # ADD VALUE can't run inside a transaction block on older Postgres.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            for stmt in statements:
                conn.execute(text(stmt))
        logger.info("Notification enum migrations applied.")
    except Exception as e:
        logger.error(f"Error applying notification enum migrations: {e}")


if __name__ == "__main__":
    prepare_database()
    start_consumer()

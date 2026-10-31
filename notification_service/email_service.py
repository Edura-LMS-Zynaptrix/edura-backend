import base64
import logging
import os
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)

# Email goes out through Resend (https://resend.com), over its HTTPS API.
RESEND_API_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
# Must be an address on a domain verified in Resend (or onboarding@resend.dev while testing).
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "Edura <onboarding@resend.dev>")
RESEND_TIMEOUT_SECONDS = float(os.getenv("RESEND_TIMEOUT_SECONDS", "15"))


class EmailDeliveryError(Exception):
    """Resend did not accept the message (not configured, rejected, or unreachable)."""


def render_email(template_name: str, context: dict) -> str:
    """
    Renders Jinja2 HTML email template with provided context dictionary.
    """
    template = jinja_env.get_template(template_name)
    return template.render(**context)


def send_email(
    to_email: str, subject: str, html_body: str, attachments: list[dict] | None = None
) -> None:
    """
    Sends an HTML email through the Resend API.

    attachments: [{"filename": str, "data": bytes}] (any extra keys are ignored).
    Raises EmailDeliveryError when the message was not accepted.
    """
    if not RESEND_API_KEY:
        raise EmailDeliveryError("RESEND_API_KEY is not set")

    payload: dict = {
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }
    if attachments:
        payload["attachments"] = [
            {
                "filename": item["filename"],
                "content": base64.b64encode(item["data"]).decode("ascii"),
            }
            for item in attachments
        ]

    logger.info(f"Sending email '{subject}' to {to_email} via Resend...")
    try:
        response = httpx.post(
            RESEND_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            timeout=RESEND_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise EmailDeliveryError(f"Could not reach Resend: {exc!r}") from exc

    if response.status_code >= 400:
        try:
            reason = response.json().get("message") or response.text
        except ValueError:
            reason = response.text
        raise EmailDeliveryError(
            f"Resend rejected the email ({response.status_code}): {reason}"
        )
    logger.info(f"Successfully sent email '{subject}' to {to_email}")

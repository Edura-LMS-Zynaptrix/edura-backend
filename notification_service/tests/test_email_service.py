import base64
from unittest.mock import MagicMock, patch

import email_service
import httpx
import pytest
from email_service import EmailDeliveryError, send_email


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(email_service, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(
        email_service, "RESEND_FROM_EMAIL", "Edura <noreply@edura.test>"
    )


def _response(status=200, body=None):
    response = MagicMock(status_code=status, text=str(body))
    response.json.return_value = body or {"id": "abc"}
    return response


def test_given_api_key_when_email_sent_then_request_is_posted_to_resend_with_the_key():
    # Act
    with patch("email_service.httpx.post", return_value=_response()) as post:
        send_email(
            to_email="recipient@example.com",
            subject="Test Subject",
            html_body="<h1>Test</h1>",
        )

    # Assert
    post.assert_called_once()
    assert post.call_args.args[0] == "https://api.resend.com/emails"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer re_test_key"}
    assert post.call_args.kwargs["json"] == {
        "from": "Edura <noreply@edura.test>",
        "to": ["recipient@example.com"],
        "subject": "Test Subject",
        "html": "<h1>Test</h1>",
    }


def test_given_pdf_when_email_sent_then_attachment_is_included_in_the_request():
    # Arrange
    with patch("email_service.httpx.post", return_value=_response()) as post:
        send_email(
            to_email="recipient@example.com",
            subject="Report",
            html_body="<p>Attached</p>",
            attachments=[
                {"filename": "report.pdf", "data": b"%PDF-1.4 test", "subtype": "pdf"}
            ],
        )

    (attachment,) = post.call_args.kwargs["json"]["attachments"]

    # Act and assert
    assert attachment["filename"] == "report.pdf"
    assert base64.b64decode(attachment["content"]) == b"%PDF-1.4 test"


def test_given_no_attachments_when_email_sent_then_request_has_no_attachments_key():
    # Act
    with patch("email_service.httpx.post", return_value=_response()) as post:
        send_email("a@example.com", "Hi", "<p>x</p>")

    # Assert
    assert "attachments" not in post.call_args.kwargs["json"]


def test_given_missing_api_key_when_email_sent_then_delivery_error_is_raised_without_calling_resend(
    monkeypatch,
):
    # Arrange
    monkeypatch.setattr(email_service, "RESEND_API_KEY", "")

    # Act
    with patch("email_service.httpx.post") as post:
        with pytest.raises(EmailDeliveryError, match="RESEND_API_KEY"):
            send_email("a@example.com", "Hi", "<p>x</p>")

    # Assert
    post.assert_not_called()


def test_given_email_rejected_by_resend_when_sent_then_delivery_error_carries_resends_reason():
    rejected = _response(403, {"message": "The domain is not verified"})
    with patch("email_service.httpx.post", return_value=rejected):
        with pytest.raises(EmailDeliveryError, match="403.*domain is not verified"):
            send_email("a@example.com", "Hi", "<p>x</p>")


def test_given_unreachable_resend_when_email_sent_then_delivery_error_is_raised():
    with patch("email_service.httpx.post", side_effect=httpx.ConnectError("down")):
        with pytest.raises(EmailDeliveryError, match="Could not reach Resend"):
            send_email("a@example.com", "Hi", "<p>x</p>")

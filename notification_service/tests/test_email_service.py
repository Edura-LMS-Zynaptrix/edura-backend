from unittest.mock import MagicMock, patch

from email_service import send_email


def test_send_email_smtp_dispatch():
    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_smtp_inst = MagicMock()
        mock_smtp_cls.return_value = mock_smtp_inst

        html_content = "<h1>Test</h1>"
        send_email(
            to_email="recipient@example.com",
            subject="Test Subject",
            html_body=html_content,
        )

        mock_smtp_cls.assert_called_once()
        mock_smtp_inst.sendmail.assert_called_once()
        mock_smtp_inst.quit.assert_called_once()


def test_send_email_with_credentials_and_tls():
    with (
        patch("smtplib.SMTP") as mock_smtp_cls,
        patch("email_service.SMTP_USER", "user@example.com"),
        patch("email_service.SMTP_PASSWORD", "secret"),
        patch("email_service.SMTP_USE_TLS", True),
    ):
        mock_smtp_inst = MagicMock()
        mock_smtp_cls.return_value = mock_smtp_inst

        send_email(
            to_email="recipient@example.com",
            subject="TLS Test",
            html_body="<p>TLS Body</p>",
        )

        mock_smtp_inst.starttls.assert_called_once()
        mock_smtp_inst.login.assert_called_once_with("user@example.com", "secret")
        mock_smtp_inst.sendmail.assert_called_once()

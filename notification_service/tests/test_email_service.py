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


def test_send_email_with_pdf_attachment():
    import email

    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_smtp_inst = MagicMock()
        mock_smtp_cls.return_value = mock_smtp_inst

        send_email(
            to_email="recipient@example.com",
            subject="Report",
            html_body="<p>Attached</p>",
            attachments=[
                {"filename": "report.pdf", "data": b"%PDF-1.4 test", "subtype": "pdf"}
            ],
        )

        raw = mock_smtp_inst.sendmail.call_args[0][2]
        msg = email.message_from_string(raw)
        assert msg.get_content_type() == "multipart/mixed"
        parts = [p for p in msg.walk() if p.get_filename()]
        assert [p.get_filename() for p in parts] == ["report.pdf"]
        assert parts[0].get_content_type() == "application/pdf"
        assert parts[0].get_payload(decode=True) == b"%PDF-1.4 test"
        assert any(p.get_content_type() == "text/html" for p in msg.walk())

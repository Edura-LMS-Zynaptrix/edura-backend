from email_service import render_email


def test_given_otp_data_when_template_rendered_then_otp_email_is_built():
    # Arrange
    context = {"otp_code": "849201", "expires_in_minutes": 5}

    # Act
    html = render_email("otp_email.html", context)

    # Assert
    assert "849201" in html
    assert "5 minutes" in html
    assert "Edura Verification" in html


def test_given_payment_data_when_template_rendered_then_payment_confirmation_is_built():
    # Arrange
    context = {
        "order_id": "ORD-998822",
        "course_title": "Advanced Microservices Architecture",
        "amount": 149.99,
    }

    # Act
    html = render_email("payment_confirm.html", context)

    # Assert
    assert "ORD-998822" in html
    assert "Advanced Microservices Architecture" in html
    assert "$149.99" in html
    assert "Payment Successful!" in html


def test_given_certificate_data_when_template_rendered_then_certificate_email_is_built():
    # Arrange
    context = {
        "student_name": "Jane Doe",
        "course_title": "Full-Stack Web Development",
        "issued_at": "2026-08-24",
        "download_url": "https://edura.com/certs/123",
    }

    # Act
    html = render_email("cert_issued.html", context)

    # Assert
    assert "Jane Doe" in html
    assert "Full-Stack Web Development" in html
    assert "2026-08-24" in html
    assert "https://edura.com/certs/123" in html


def test_given_violation_data_when_template_rendered_then_alert_email_is_built():
    # Arrange
    context = {"violation_details": "Multiple faces detected in camera feed."}

    # Act
    html = render_email("violation_alert.html", context)

    # Assert
    assert "Multiple faces detected in camera feed." in html
    assert "Academic Integrity Alert" in html

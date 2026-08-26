from email_service import render_email


def test_render_otp_email():
    context = {"otp_code": "849201", "expires_in_minutes": 5}
    html = render_email("otp_email.html", context)
    assert "849201" in html
    assert "5 minutes" in html
    assert "Edura Verification" in html


def test_render_payment_confirm():
    context = {
        "order_id": "ORD-998822",
        "course_title": "Advanced Microservices Architecture",
        "amount": 149.99,
    }
    html = render_email("payment_confirm.html", context)
    assert "ORD-998822" in html
    assert "Advanced Microservices Architecture" in html
    assert "$149.99" in html
    assert "Payment Successful!" in html


def test_render_cert_issued():
    context = {
        "student_name": "Jane Doe",
        "course_title": "Full-Stack Web Development",
        "issued_at": "2026-08-24",
        "download_url": "https://edura.com/certs/123",
    }
    html = render_email("cert_issued.html", context)
    assert "Jane Doe" in html
    assert "Full-Stack Web Development" in html
    assert "2026-08-24" in html
    assert "https://edura.com/certs/123" in html


def test_render_violation_alert():
    context = {"violation_details": "Multiple faces detected in camera feed."}
    html = render_email("violation_alert.html", context)
    assert "Multiple faces detected in camera feed." in html
    assert "Academic Integrity Alert" in html

"""Feedback: anyone may submit, only admins may list, mark read or delete."""

import pytest
from conftest import ADMIN, STUDENT, auth

VALID = {
    "name": "Nimal Perera",
    "email": "nimal@example.com",
    "category": "bug",
    "subject": "Video won't load",
    "message": "The lesson video stays on a black screen.",
}


def submit(client, headers=None, **overrides):
    return client.post(
        "/feedback/submit", json={**VALID, **overrides}, headers=headers or {}
    )


# ---------------------------------------------------------------------------
# /admin
# ---------------------------------------------------------------------------


def test_admin_route_welcomes_admins(client):
    response = client.get("/admin", headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome Admin"}


def test_admin_role_check_ignores_case(client):
    assert client.get("/admin", headers=auth("ADMIN", 1)).status_code == 200


def test_admin_route_rejects_students_and_anonymous(client):
    assert client.get("/admin", headers=STUDENT).status_code == 403
    assert client.get("/admin").status_code == 401


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "service": "admin_service"}


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def test_guest_can_submit_feedback(client):
    response = submit(client)
    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] is None
    assert body["name"] == "Nimal Perera"
    assert body["category"] == "bug"
    assert body["is_read"] is False
    assert body["is_resolved"] is False
    assert body["created_at"]


def test_signed_in_sender_is_linked_by_user_id(client):
    assert submit(client, headers=STUDENT).json()["user_id"] == 7


def test_invalid_token_is_treated_as_guest(client):
    response = submit(client, headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 201
    assert response.json()["user_id"] is None


def test_submit_trims_text_and_drops_a_blank_subject(client):
    body = submit(
        client, name="  Nimal  ", subject="   ", message="  Hello there  "
    ).json()
    assert body["name"] == "Nimal"
    assert body["subject"] is None
    assert body["message"] == "Hello there"


def test_subject_is_optional(client):
    payload = {k: v for k, v in VALID.items() if k != "subject"}
    response = client.post("/feedback/submit", json=payload)
    assert response.status_code == 201
    assert response.json()["subject"] is None


@pytest.mark.parametrize("category", ["suggestion", "bug", "other"])
def test_every_category_is_accepted(client, category):
    assert submit(client, category=category).status_code == 201


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"category": "complaint"},
        {"name": ""},
        {"name": "x" * 121},
        {"message": ""},
        {"message": "x" * 5001},
        {"subject": "x" * 201},
    ],
    ids=[
        "bad-email",
        "unknown-category",
        "empty-name",
        "long-name",
        "empty-message",
        "long-message",
        "long-subject",
    ],
)
def test_submit_rejects_invalid_input(client, overrides):
    assert submit(client, **overrides).status_code == 422


def test_submit_requires_name_email_category_and_message(client):
    for field in ("name", "email", "category", "message"):
        payload = {k: v for k, v in VALID.items() if k != field}
        assert client.post("/feedback/submit", json=payload).status_code == 422, field


# ---------------------------------------------------------------------------
# Admin triage
# ---------------------------------------------------------------------------


def test_listing_feedback_is_admin_only(client):
    assert client.get("/feedback/admin/all").status_code == 401
    assert client.get("/feedback/admin/all", headers=STUDENT).status_code == 403


def test_list_returns_newest_first(client):
    ids = [submit(client, subject=f"#{i}").json()["id"] for i in range(3)]
    listed = client.get("/feedback/admin/all", headers=ADMIN).json()
    assert [f["id"] for f in listed] == ids[::-1]


def test_list_supports_skip_and_limit(client):
    ids = [submit(client).json()["id"] for _ in range(5)]
    page = client.get("/feedback/admin/all?skip=1&limit=2", headers=ADMIN).json()
    assert [f["id"] for f in page] == ids[::-1][1:3]


@pytest.mark.parametrize("query", ["skip=-1", "limit=0", "limit=501"])
def test_list_rejects_out_of_range_paging(client, query):
    assert client.get(f"/feedback/admin/all?{query}", headers=ADMIN).status_code == 422


def test_admin_can_mark_feedback_read(client):
    feedback_id = submit(client).json()["id"]
    response = client.patch(f"/feedback/admin/{feedback_id}/read", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["is_read"] is True
    listed = client.get("/feedback/admin/all", headers=ADMIN).json()
    assert listed[0]["is_read"] is True


def test_marking_unknown_feedback_read_is_404(client):
    response = client.patch("/feedback/admin/999/read", headers=ADMIN)
    assert response.status_code == 404
    assert response.json()["detail"] == {"error": "FEEDBACK_NOT_FOUND"}


def test_students_cannot_mark_feedback_read(client):
    feedback_id = submit(client).json()["id"]
    response = client.patch(f"/feedback/admin/{feedback_id}/read", headers=STUDENT)
    assert response.status_code == 403


def test_admin_can_delete_feedback(client):
    feedback_id = submit(client).json()["id"]
    response = client.delete(f"/feedback/admin/{feedback_id}", headers=ADMIN)
    assert response.status_code == 204
    assert client.get("/feedback/admin/all", headers=ADMIN).json() == []
    assert (
        client.delete(f"/feedback/admin/{feedback_id}", headers=ADMIN).status_code
        == 404
    )


def test_students_cannot_delete_feedback(client):
    feedback_id = submit(client).json()["id"]
    assert (
        client.delete(f"/feedback/admin/{feedback_id}", headers=STUDENT).status_code
        == 403
    )
    assert len(client.get("/feedback/admin/all", headers=ADMIN).json()) == 1

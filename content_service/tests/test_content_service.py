"""Content service gaps: course material uploads, asset deletion, avatars, the
Cloudinary helpers, the access check against the local copy, the consumer and
startup. Cloudinary and libmagic are replaced with stand-ins."""

import asyncio

import access
import cloudinary_utils
import event_consumer
import jwt
import main
import pytest
import router
from database import Base
from fastapi import HTTPException
from fastapi.testclient import TestClient
from main import app
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import auth
from shared import events as ev

client = TestClient(app)

PDF = b"%PDF-1.4 material"
PNG = b"\x89PNG\r\n\x1a\n0000"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _auth(user_id: int, role: str) -> dict:
    token = jwt.encode(
        {"sub": str(user_id), "role": role}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
    )
    return {"Authorization": f"Bearer {token}"}


TEACHER = _auth(5, "teacher")
ADMIN = _auth(1, "admin")
STUDENT = _auth(101, "student")


@pytest.fixture
def detected(monkeypatch):
    """What libmagic will report for the next upload (defaults to the declared type)."""
    reported = {"mime": None}

    def from_buffer(content, mime=True):
        return reported["mime"]

    monkeypatch.setattr(router.magic, "from_buffer", from_buffer)
    return reported


@pytest.fixture
def uploads(monkeypatch):
    calls = []

    def upload_asset(content, filename, resource_type="auto", folder="edura/uploads"):
        calls.append(
            {"filename": filename, "resource_type": resource_type, "folder": folder}
        )
        return f"{folder}/{filename}", f"https://cdn/{folder}/{filename}"

    monkeypatch.setattr(router, "upload_asset", upload_asset)
    return calls


def _post(url, content, mime, name="file.bin", headers=TEACHER):
    return client.post(url, files={"file": (name, content, mime)}, headers=headers)


# ---------------------------------------------------------------------------
# App wiring and the access copy
# ---------------------------------------------------------------------------


def test_startup_creates_tables_and_starts_consumer(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    calls = []
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))
    main.on_startup()
    with engine.connect() as conn:
        assert "replica_access" in engine.dialect.get_table_names(conn)
    assert calls == ["consumer"]
    engine.dispose()


def test_startup_survives_create_all_failure(monkeypatch):
    calls = []

    def broken(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.Base.metadata, "create_all", broken)
    monkeypatch.setattr(event_consumer, "start", lambda: calls.append("consumer"))
    main.on_startup()
    assert calls == ["consumer"]


@pytest.fixture
def replica(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(access, "SessionLocal", factory)
    monkeypatch.setattr(event_consumer, "SessionLocal", factory)
    yield
    engine.dispose()


def test_access_comes_from_access_events(replica):
    assert asyncio.run(access.is_enrolled(101, 10)) is False
    event_consumer.handle(
        "access.changed",
        {
            "student_id": 101,
            "courses": [
                {"id": 10, "until": None},
                {"id": 11, "until": "2020-01-01T00:00:00+00:00"},
            ],
            "lessons": [99],
        },
    )
    assert asyncio.run(access.is_enrolled(101, 10)) is True
    assert asyncio.run(access.is_enrolled(101, 11)) is False  # expired grant


def test_enrolled_student_streams_through_the_real_access_check(replica):
    event_consumer.handle(
        "access.changed",
        {"student_id": 101, "courses": [{"id": 10, "until": None}], "lessons": []},
    )
    ok = client.get(
        "/lessons/1/stream?youtube_video_id=abc&course_id=10", headers=STUDENT
    )
    assert ok.status_code == 200
    assert ok.json() == {
        "embed_url": "https://www.youtube-nocookie.com/embed/abc?rel=0&modestbranding=1",
        "allow_origin": "https://edura.lk",
    }
    denied = client.get(
        "/lessons/1/stream?youtube_video_id=abc&course_id=11", headers=STUDENT
    )
    assert denied.status_code == 403


def test_consumer_start_and_rollback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        event_consumer.messaging, "start_consumer", lambda *args: calls.append(args)
    )
    event_consumer.start()
    assert calls == [
        ("content", "content.queue", ev.QUEUES["content.queue"], event_consumer.handle)
    ]

    class FakeSession:
        rolled_back = closed = False

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    fake = FakeSession()

    def explode(*args, **kwargs):
        raise ValueError("bad event")

    monkeypatch.setattr(event_consumer, "SessionLocal", lambda: fake)
    monkeypatch.setattr(event_consumer.replicas, "apply", explode)
    with pytest.raises(ValueError):
        event_consumer.handle("access.changed", {})
    assert fake.rolled_back and fake.closed


# ---------------------------------------------------------------------------
# POST /upload-material
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared, reported, resource_type",
    [
        ("application/pdf", "application/pdf", "raw"),
        (DOCX, "application/zip", "raw"),  # docx is a zip container
        ("application/msword", "application/CDFV2", "raw"),
        ("application/x-zip-compressed", "application/zip", "raw"),
        ("image/png", "image/png", "image"),
    ],
)
def test_material_upload(detected, uploads, declared, reported, resource_type):
    detected["mime"] = reported
    resp = _post("/upload-material", PDF, declared, name="notes.docx")
    assert resp.status_code == 200
    data = resp.json()
    # The declared type is reported back (libmagic can't tell docx from zip).
    assert data["mime_type"] == declared
    assert data["message"] == "Material uploaded successfully"
    assert uploads == [
        {
            "filename": "notes.docx",
            "resource_type": resource_type,
            "folder": "edura/uploads",
        }
    ]


def test_material_upload_rejections(detected, uploads, monkeypatch):
    empty = _post("/upload-material", b"", "application/pdf")
    assert (empty.status_code, empty.json()["detail"]["error"]) == (422, "EMPTY_FILE")

    wrong_type = _post("/upload-material", PDF, "text/html")
    assert wrong_type.json()["detail"]["error"] == "UNSUPPORTED_MEDIA_TYPE"

    detected["mime"] = "application/x-dosexec"
    disguised = _post("/upload-material", PDF, "application/pdf")
    assert (disguised.status_code, disguised.json()["detail"]["error"]) == (
        415,
        "MIME_MISMATCH",
    )

    monkeypatch.setattr(router, "MAX_MATERIAL_SIZE", 4)
    too_big = _post("/upload-material", PDF, "application/pdf")
    assert (too_big.status_code, too_big.json()["detail"]["error"]) == (
        413,
        "FILE_TOO_LARGE",
    )
    assert uploads == []


def test_material_upload_is_for_staff(uploads):
    assert (
        _post("/upload-material", PDF, "application/pdf", headers=STUDENT).status_code
        == 403
    )
    assert (
        _post("/upload-material", PDF, "application/pdf", headers={}).status_code == 401
    )


# ---------------------------------------------------------------------------
# DELETE /assets
# ---------------------------------------------------------------------------


def test_delete_uploaded_asset(monkeypatch):
    deleted = []
    monkeypatch.setattr(
        router, "delete_asset", lambda pid: deleted.append(pid) or False
    )
    resp = client.delete("/assets?public_id=edura/uploads/notes", headers=ADMIN)
    # An asset that is already gone is not an error.
    assert resp.status_code == 204
    assert deleted == ["edura/uploads/notes"]


@pytest.mark.parametrize(
    "public_id",
    [
        "edura/avatars/me",
        "edura/receipts/slip",
        "edura/uploads/../receipts/slip",
        "other",
    ],
)
def test_only_content_assets_can_be_deleted(monkeypatch, public_id):
    monkeypatch.setattr(router, "delete_asset", lambda pid: pytest.fail("deleted"))
    resp = client.delete(f"/assets?public_id={public_id}", headers=TEACHER)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "ASSET_NOT_DELETABLE"


def test_delete_asset_validation_and_roles():
    assert client.delete("/assets?public_id=", headers=TEACHER).status_code == 422
    assert (
        client.delete("/assets?public_id=edura/uploads/x", headers=STUDENT).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# POST /avatar
# ---------------------------------------------------------------------------


def test_any_user_can_upload_an_avatar(detected, uploads):
    detected["mime"] = "image/png"
    resp = _post("/avatar", PNG, "image/png", name="me.png", headers=STUDENT)
    assert resp.status_code == 200
    assert resp.json()["message"] == "Avatar uploaded successfully"
    assert uploads[0]["folder"] == "edura/avatars"


def test_avatar_rejections(detected, uploads, monkeypatch):
    assert (
        _post("/avatar", PDF, "application/pdf", headers=STUDENT).json()["detail"][
            "error"
        ]
        == "UNSUPPORTED_MEDIA_TYPE"
    )
    detected["mime"] = "image/gif"
    mismatch = _post("/avatar", PNG, "image/png", headers=STUDENT)
    assert mismatch.json()["detail"]["error"] == "MIME_MISMATCH"
    monkeypatch.setattr(router, "MAX_AVATAR_SIZE", 2)
    assert _post("/avatar", PNG, "image/png", headers=STUDENT).status_code == 413
    assert _post("/avatar", PNG, "image/png", headers={}).status_code == 401
    assert uploads == []


# ---------------------------------------------------------------------------
# Cloudinary helpers
# ---------------------------------------------------------------------------


def test_missing_credentials_are_a_503(monkeypatch):
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "")
    with pytest.raises(HTTPException) as err:
        cloudinary_utils.upload_asset(b"x", "x.pdf")
    assert err.value.status_code == 503
    assert err.value.detail["error"] == "CLOUDINARY_NOT_CONFIGURED"


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "demo")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "key")
    monkeypatch.setenv("CLOUDINARY_API_SECRET", "secret")
    calls = []
    monkeypatch.setattr(
        cloudinary_utils.cloudinary, "config", lambda **kw: calls.append(("config", kw))
    )
    return calls


def test_upload_asset_passes_safe_options(cloud, monkeypatch):
    def upload(content, **options):
        cloud.append(("upload", options))
        return {"public_id": "edura/uploads/a", "secure_url": "https://cdn/a"}

    monkeypatch.setattr(cloudinary_utils.cloudinary.uploader, "upload", upload)
    assert cloudinary_utils.upload_asset(b"x", "a.pdf", resource_type="raw") == (
        "edura/uploads/a",
        "https://cdn/a",
    )
    (_, config), (_, options) = cloud
    assert config == {
        "cloud_name": "demo",
        "api_key": "key",
        "api_secret": "secret",
        "secure": True,
    }
    assert options["overwrite"] is False and options["unique_filename"] is True
    assert (options["folder"], options["resource_type"]) == ("edura/uploads", "raw")


def test_delete_asset_tries_each_resource_type(cloud, monkeypatch):
    tried = []

    def destroy(public_id, resource_type, invalidate):
        tried.append(resource_type)
        return {"result": "ok" if resource_type == "raw" else "not found"}

    monkeypatch.setattr(cloudinary_utils.cloudinary.uploader, "destroy", destroy)
    assert cloudinary_utils.delete_asset("edura/uploads/a") is True
    assert tried == ["image", "raw"]

    tried.clear()
    monkeypatch.setattr(
        cloudinary_utils.cloudinary.uploader,
        "destroy",
        lambda *a, **k: tried.append(k["resource_type"]) or {"result": "not found"},
    )
    assert cloudinary_utils.delete_asset("edura/uploads/gone") is False
    assert tried == ["image", "raw", "video"]


def test_signed_url_expires_in_an_hour(cloud, monkeypatch):
    seen = {}

    def cloudinary_url(public_id, **options):
        seen.update(options, public_id=public_id)
        return f"https://cdn/signed/{public_id}", options

    monkeypatch.setattr(cloudinary_utils.time, "time", lambda: 1_000_000)
    monkeypatch.setattr(
        cloudinary_utils.cloudinary.utils, "cloudinary_url", cloudinary_url
    )
    url, expires = cloudinary_utils.get_signed_url("edura/uploads/a")
    assert (url, expires) == ("https://cdn/signed/edura/uploads/a", 1_003_600)
    assert seen["sign_url"] is True and seen["expires_at"] == 1_003_600

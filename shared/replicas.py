"""Replicas: the small copies of other services' data that a service keeps in its own database.

A service never asks another service for data. Instead the owning service
announces each change (``shared/events.py``) and every service that needs the
data stores the latest state locally, in ``replica_*`` tables. Those tables are
read-only as far as the service's own logic goes: only ``apply`` writes them.

Use ``define_*`` with the service's declarative ``Base`` to get a table class,
and ``apply`` from the consumer to keep it up to date.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from shared import events as ev


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    """An ISO timestamp from an event, as an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- tables


def define_replica_user(Base):
    """Accounts and profiles in one row (from auth.* and user.profile-* events)."""

    class ReplicaUser(Base):
        __tablename__ = "replica_users"

        user_id: Mapped[int] = mapped_column(
            Integer, primary_key=True, autoincrement=False
        )
        role: Mapped[str | None] = mapped_column(String(20), nullable=True)
        email: Mapped[str | None] = mapped_column(String(255), nullable=True)
        is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
        is_email_verified: Mapped[bool] = mapped_column(
            Boolean, nullable=False, default=False
        )
        account_created_at: Mapped[datetime | None] = mapped_column(
            DateTime(timezone=True), nullable=True
        )
        first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
        last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
        full_name: Mapped[str | None] = mapped_column(String(210), nullable=True)
        mobile_no: Mapped[str | None] = mapped_column(String(20), nullable=True)
        school: Mapped[str | None] = mapped_column(String(200), nullable=True)
        district: Mapped[str | None] = mapped_column(String(100), nullable=True)
        grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
        stream_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
        avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
        profile_created_at: Mapped[datetime | None] = mapped_column(
            DateTime(timezone=True), nullable=True
        )
        updated_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )

        @property
        def display_name(self) -> str:
            return self.full_name or self.email or ""

    return ReplicaUser


def define_replica_account(Base):
    """Just the account side (email, active flag) for a service that owns the profile itself."""

    class ReplicaAccount(Base):
        __tablename__ = "replica_accounts"

        user_id: Mapped[int] = mapped_column(
            Integer, primary_key=True, autoincrement=False
        )
        role: Mapped[str | None] = mapped_column(String(20), nullable=True)
        email: Mapped[str | None] = mapped_column(String(255), nullable=True)
        is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
        is_email_verified: Mapped[bool] = mapped_column(
            Boolean, nullable=False, default=False
        )
        account_created_at: Mapped[datetime | None] = mapped_column(
            DateTime(timezone=True), nullable=True
        )
        updated_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )

    return ReplicaAccount


def define_replica_course(Base):
    class ReplicaCourse(Base):
        __tablename__ = "replica_courses"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
        title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
        grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
        stream_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
        course_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
        status: Mapped[str | None] = mapped_column(String(20), nullable=True)
        price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
        instructor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
        thumbnail_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
        updated_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )

        @property
        def is_published(self) -> bool:
            return (self.status or "").lower() == "published"

    return ReplicaCourse


def define_replica_module(Base):
    class ReplicaModule(Base):
        __tablename__ = "replica_modules"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
        course_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
        title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
        position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
        is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    return ReplicaModule


def define_replica_access(Base):
    """What each student can open, as announced by enrollment_service."""

    class ReplicaAccess(Base):
        __tablename__ = "replica_access"

        student_id: Mapped[int] = mapped_column(
            Integer, primary_key=True, autoincrement=False
        )
        # [{"id": 3, "until": "2027-01-01T00:00:00+00:00" | null}, ...] — whole courses
        courses: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
        # [12, 15, ...] — single lessons (modules) granted on their own
        lessons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
        updated_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )

    return ReplicaAccess


def define_replica_stream(Base):
    class ReplicaStream(Base):
        __tablename__ = "replica_streams"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
        name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
        description: Mapped[str | None] = mapped_column(Text, nullable=True)
        subjects: Mapped[list | None] = mapped_column(JSON, nullable=True)
        is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    return ReplicaStream


# ---------------------------------------------------------------- reading


def accessible(access_row, now: datetime | None = None) -> tuple[set[int], set[int]]:
    """(course ids open right now, lesson ids granted on their own) for one student."""
    if access_row is None:
        return set(), set()
    now = now or _utcnow()
    courses = set()
    for grant in access_row.courses or []:
        until = parse_time(grant.get("until"))
        if until is None or until > now:
            courses.add(int(grant["id"]))
    return courses, {int(m) for m in (access_row.lessons or [])}


def has_access(
    db: Session, Access, student_id: int, course_id: int, module_id: int | None = None
) -> bool:
    """May this student open the course (or, with module_id, that one lesson)?"""
    courses, lessons = accessible(db.get(Access, student_id))
    if course_id in courses:
        return True
    return module_id is not None and module_id in lessons


# ---------------------------------------------------------------- writing


def _upsert(db: Session, Model, key: Any, **fields):
    row = db.get(Model, key)
    if row is None:
        pk = Model.__mapper__.primary_key[0].key
        row = Model(**{pk: key})
        db.add(row)
    for name, value in fields.items():
        setattr(row, name, value)
    return row


def _full_name(first: str | None, last: str | None) -> str | None:
    name = f"{first or ''} {last or ''}".strip()
    return name or None


def _account_event(db, data, tables):
    fields = {
        name: data[name]
        for name in ("email", "role", "is_active", "is_email_verified")
        if name in data
    }
    if data.get("created_at"):
        fields["account_created_at"] = parse_time(data["created_at"])
    done = False
    for table in (tables["user"], tables["account"]):
        if table is not None:
            _upsert(db, table, int(data["user_id"]), **fields)
            done = True
    return done


_PROFILE_FIELDS = (
    "role",
    "first_name",
    "last_name",
    "mobile_no",
    "school",
    "district",
    "grade",
    "stream_id",
    "avatar_url",
)


def _profile_upserted(db, data, tables):
    user = tables["user"]
    if user is None:
        return False
    fields = {name: data.get(name) for name in _PROFILE_FIELDS}
    fields["full_name"] = data.get("full_name") or _full_name(
        data.get("first_name"), data.get("last_name")
    )
    fields["profile_created_at"] = parse_time(data.get("created_at"))
    _upsert(db, user, int(data["user_id"]), **fields)
    return True


def _delete(table_key: str, id_field: str):
    def handler(db, data, tables):
        table = tables[table_key]
        if table is None:
            return False
        row = db.get(table, int(data[id_field]))
        if row is not None:
            db.delete(row)
        return True

    return handler


def _course_upserted(db, data, tables):
    course = tables["course"]
    if course is None:
        return False
    _upsert(
        db,
        course,
        int(data["id"]),
        title=data.get("title") or "",
        grade=data.get("grade"),
        stream_ids=data.get("stream_ids") or [],
        course_type=data.get("course_type"),
        status=data.get("status"),
        price=data.get("price") or 0,
        instructor_id=data.get("instructor_id"),
        thumbnail_url=data.get("thumbnail_url"),
    )
    return True


def _module_upserted(db, data, tables):
    module = tables["module"]
    if module is None:
        return False
    _upsert(
        db,
        module,
        int(data["id"]),
        course_id=int(data["course_id"]),
        title=data.get("title") or "",
        position=data.get("position") or 0,
        is_active=bool(data.get("is_active", True)),
    )
    return True


def _access_changed(db, data, tables):
    access = tables["access"]
    if access is None:
        return False
    _upsert(
        db,
        access,
        int(data["student_id"]),
        courses=data.get("courses") or [],
        lessons=data.get("lessons") or [],
    )
    return True


def _stream_upserted(db, data, tables):
    stream = tables["stream"]
    if stream is None:
        return False
    _upsert(
        db,
        stream,
        int(data["id"]),
        name=data.get("name") or "",
        description=data.get("description"),
        subjects=data.get("subjects") or [],
        is_active=bool(data.get("is_active", True)),
    )
    return True


_HANDLERS = {
    ev.AUTH_USER_REGISTERED: _account_event,
    ev.AUTH_ACCOUNT_UPDATED: _account_event,
    ev.USER_PROFILE_UPSERTED: _profile_upserted,
    ev.USER_PROFILE_DELETED: _delete("user", "user_id"),
    ev.COURSE_UPSERTED: _course_upserted,
    ev.COURSE_DELETED: _delete("course", "id"),
    ev.COURSE_MODULE_UPSERTED: _module_upserted,
    ev.COURSE_MODULE_DELETED: _delete("module", "id"),
    ev.ACCESS_CHANGED: _access_changed,
    ev.USER_STREAM_UPSERTED: _stream_upserted,
    ev.USER_STREAM_DELETED: _delete("stream", "id"),
}


def apply(
    db: Session,
    key: str,
    data: dict,
    *,
    user=None,
    account=None,
    course=None,
    module=None,
    access=None,
    stream=None,
) -> bool:
    """Apply one event to whichever replicas this service keeps. True when it changed one."""
    handler = _HANDLERS.get(key)
    if handler is None:
        return False
    tables = {
        "user": user,
        "account": account,
        "course": course,
        "module": module,
        "access": access,
        "stream": stream,
    }
    handled = handler(db, data, tables)
    if handled:
        db.commit()
    return handled

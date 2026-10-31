"""The event catalogue: every routing key on the ``edura.events`` exchange, and which service listens to what.

Services never call each other over HTTP. A service that owns some data
announces every change as an event; a service that needs that data keeps its own
small replica (``shared/replicas.py``) and updates it from these events.

``QUEUES`` is the single source of truth for the broker topology. The infra
repository's ``rabbitmq/setup.py`` declares the same queues and bindings.
"""

EXCHANGE = "edura.events"

# ---- auth_service: accounts -------------------------------------------------
AUTH_USER_REGISTERED = "auth.user-registered"
AUTH_ACCOUNT_UPDATED = "auth.account-updated"
AUTH_OTP_REQUESTED = "auth.otp-requested"
AUTH_EMAIL_VERIFICATION = "auth.email-verification-requested"
AUTH_PASSWORD_RESET = "auth.password-reset-requested"
AUTH_PASSWORD_CHANGED = "auth.password-changed"

# ---- user_service: profiles and streams ------------------------------------
USER_PROFILE_UPSERTED = "user.profile-upserted"
USER_PROFILE_DELETED = "user.profile-deleted"
USER_STREAM_UPSERTED = "user.stream-upserted"
USER_STREAM_DELETED = "user.stream-deleted"

# ---- course_service: catalogue ---------------------------------------------
COURSE_UPSERTED = "course.upserted"
COURSE_DELETED = "course.deleted"
COURSE_PUBLISHED = "course.published"
COURSE_MODULE_UPSERTED = "course.module-upserted"
COURSE_MODULE_DELETED = "course.module-deleted"

# ---- enrollment_service: who can open what ---------------------------------
ENROLLMENT_ACTIVATED = "enrollment.activated"
ENROLLMENT_CHANGED = "enrollment.changed"
ACCESS_CHANGED = "access.changed"
SUBSCRIPTION_EXPIRED = "subscription.expired"

# ---- payment_service --------------------------------------------------------
PAYMENT_SUCCESS = "payment.success"
PAYMENT_RECEIPT_UPLOADED = "payment.receipt-uploaded"
PAYMENT_RECEIPT_REVIEWED = "payment.receipt-reviewed"

# ---- assessment_service -----------------------------------------------------
ASSESSMENT_UPSERTED = "assessment.upserted"
ASSESSMENT_DELETED = "assessment.deleted"
ASSESSMENT_GRADED = "assessment.graded"
BADGE_AWARDED = "badge.awarded"

# ---- analytics / misc -------------------------------------------------------
REPORT_PROGRESS = "report.progress"
CERTIFICATE_ISSUED = "certificate.issued"
PROCTORING_VIOLATION = "proctoring.violation"
VIOLATION_ALERT = "violation.alert"
LESSON_WATCHED = "lesson.watched"

# Reference data that more than one service keeps a replica of.
_CATALOGUE = [COURSE_UPSERTED, COURSE_DELETED]
_LESSONS = [COURSE_MODULE_UPSERTED, COURSE_MODULE_DELETED]
_PROFILES = [USER_PROFILE_UPSERTED, USER_PROFILE_DELETED]
_ACCOUNTS = [AUTH_USER_REGISTERED, AUTH_ACCOUNT_UPDATED]

# queue -> routing keys it is bound to. One durable queue per consuming service.
QUEUES: dict[str, list[str]] = {
    "user.queue": [*_ACCOUNTS, ENROLLMENT_ACTIVATED],
    "course.queue": [ACCESS_CHANGED],
    "content.queue": [ACCESS_CHANGED],
    "enrollment.queue": [
        PAYMENT_SUCCESS,
        SUBSCRIPTION_EXPIRED,
        *_CATALOGUE,
        *_PROFILES,
        USER_STREAM_UPSERTED,
        USER_STREAM_DELETED,
    ],
    "payment.queue": list(_CATALOGUE),
    "assessment.queue": [
        ACCESS_CHANGED,
        *_CATALOGUE,
        *_PROFILES,
        *_ACCOUNTS,
    ],
    "progress.queue": [ENROLLMENT_ACTIVATED, ASSESSMENT_GRADED, LESSON_WATCHED],
    "notification.queue": [
        AUTH_OTP_REQUESTED,
        AUTH_EMAIL_VERIFICATION,
        AUTH_PASSWORD_RESET,
        AUTH_PASSWORD_CHANGED,
        PAYMENT_SUCCESS,
        "payment.manual-uploaded",
        ASSESSMENT_GRADED,
        CERTIFICATE_ISSUED,
        PROCTORING_VIOLATION,
        VIOLATION_ALERT,
        REPORT_PROGRESS,
    ],
    "admin.queue": [*_PROFILES, *_ACCOUNTS],
    "analytics.queue": [
        *_ACCOUNTS,
        *_PROFILES,
        *_CATALOGUE,
        *_LESSONS,
        ACCESS_CHANGED,
        ENROLLMENT_CHANGED,
        PAYMENT_SUCCESS,
        PAYMENT_RECEIPT_UPLOADED,
        PAYMENT_RECEIPT_REVIEWED,
        ASSESSMENT_UPSERTED,
        ASSESSMENT_DELETED,
        ASSESSMENT_GRADED,
        BADGE_AWARDED,
    ],
}

# Queues from earlier versions that nothing reads any more; setup removes them.
OBSOLETE_QUEUES = ["user.referrals.queue"]

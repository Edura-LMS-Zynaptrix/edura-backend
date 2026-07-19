import magic
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from cloudinary_utils import get_signed_url, upload_asset
from enrollment_client import is_enrolled
from schemas import SignedUrlRequest, SignedUrlResponse, StreamResponse, UploadValidationResponse
from shared.auth import require_role

router = APIRouter()

_ALL_ROLES = ["student", "teacher", "admin"]
_TEACHER_ADMIN = ["teacher", "admin"]

ALLOWED_MIME_TYPES = {"application/pdf", "image/png", "image/jpeg"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB
YOUTUBE_EMBED_BASE = "https://www.youtube-nocookie.com/embed"
ALLOW_ORIGIN = "https://edura.lk"


# ---------------------------------------------------------------------------
# POST /signed-url
# ---------------------------------------------------------------------------

@router.post("/signed-url", response_model=SignedUrlResponse)
async def generate_signed_url(
    body: SignedUrlRequest,
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    role = payload.get("role", "")
    if role == "student":
        student_id = int(payload["sub"])
        enrolled = await is_enrolled(student_id, body.course_id)
        if not enrolled:
            raise HTTPException(
                status_code=403,
                detail={"error": "NOT_ENROLLED", "message": "Student is not enrolled in this course"},
            )

    url, expires_at = get_signed_url(body.public_id)
    return SignedUrlResponse(signed_url=url, expires_at=expires_at)


# ---------------------------------------------------------------------------
# GET /lessons/{lesson_id}/stream
# ---------------------------------------------------------------------------

@router.get("/lessons/{lesson_id}/stream", response_model=StreamResponse)
async def stream_lesson(
    lesson_id: int,
    youtube_video_id: str = Query(..., description="YouTube video ID for this lesson"),
    course_id: int = Query(None, description="Required for students — enrollment is verified"),
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    role = payload.get("role", "")
    if role == "student":
        if course_id is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "COURSE_ID_REQUIRED", "message": "course_id query parameter is required for students"},
            )
        student_id = int(payload["sub"])
        enrolled = await is_enrolled(student_id, course_id)
        if not enrolled:
            raise HTTPException(
                status_code=403,
                detail={"error": "NOT_ENROLLED", "message": "Student is not enrolled in this course"},
            )

    embed_url = f"{YOUTUBE_EMBED_BASE}/{youtube_video_id}?rel=0&modestbranding=1"
    return StreamResponse(embed_url=embed_url, allow_origin=ALLOW_ORIGIN)


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------

@router.post("/upload", response_model=UploadValidationResponse)
async def upload_file(
    file: UploadFile = File(...),
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
):
    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail={"error": "FILE_TOO_LARGE", "message": "File exceeds the 25 MB limit"},
        )

    declared_mime = file.content_type or ""
    if declared_mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "UNSUPPORTED_MEDIA_TYPE",
                "message": f"MIME type '{declared_mime}' is not allowed. Allowed: pdf, png, jpeg",
            },
        )

    detected_mime = magic.from_buffer(content, mime=True)
    if detected_mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "MIME_MISMATCH",
                "message": f"File signature indicates '{detected_mime}', which does not match declared type",
            },
        )

    public_id, secure_url = upload_asset(content, file.filename or "upload")

    return UploadValidationResponse(
        valid=True,
        filename=file.filename or "",
        size_bytes=len(content),
        mime_type=detected_mime,
        public_id=public_id,
        secure_url=secure_url,
        message="File uploaded successfully",
    )

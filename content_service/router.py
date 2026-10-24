import magic
from access import is_enrolled
from cloudinary_utils import (
    AVATAR_FOLDER,
    FOLDER,
    delete_asset,
    get_signed_url,
    upload_asset,
)
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from schemas import (
    SignedUrlRequest,
    SignedUrlResponse,
    StreamResponse,
    UploadValidationResponse,
)

from shared.auth import require_role

router = APIRouter()

_ALL_ROLES = ["student", "teacher", "admin"]
_TEACHER_ADMIN = ["teacher", "admin"]

ALLOWED_MIME_TYPES = {"application/pdf", "image/png", "image/jpeg"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB
# Downloadable course materials — documents go to Cloudinary as "raw" so
# they are delivered as-is (PDF/ZIP delivery as "image" is blocked by default).
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ALLOWED_MATERIAL_MIME_TYPES = ALLOWED_MIME_TYPES | {
    "application/msword",
    _DOCX,
    "application/zip",
    "application/x-zip-compressed",
}
# What libmagic may report for those: OOXML files are zip containers and
# legacy .doc files are OLE compound documents.
DETECTED_MATERIAL_MIME_TYPES = ALLOWED_MIME_TYPES | {
    "application/msword",
    "application/CDFV2",
    "application/x-ole-storage",
    _DOCX,
    "application/zip",
}
MAX_MATERIAL_SIZE = 20 * 1024 * 1024  # 20 MB
DELETABLE_PREFIX = f"{FOLDER}/"
ALLOWED_AVATAR_MIME_TYPES = {"image/png", "image/jpeg"}
MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5 MB
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
    role = payload.get("role", "").lower()
    if role == "student":
        student_id = int(payload["sub"])
        enrolled = await is_enrolled(student_id, body.course_id)
        if not enrolled:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "NOT_ENROLLED",
                    "message": "Student is not enrolled in this course",
                },
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
    course_id: int = Query(
        None, description="Required for students — enrollment is verified"
    ),
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    role = payload.get("role", "").lower()
    if role == "student":
        if course_id is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "COURSE_ID_REQUIRED",
                    "message": "course_id query parameter is required for students",
                },
            )
        student_id = int(payload["sub"])
        enrolled = await is_enrolled(student_id, course_id)
        if not enrolled:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "NOT_ENROLLED",
                    "message": "Student is not enrolled in this course",
                },
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
            detail={
                "error": "FILE_TOO_LARGE",
                "message": "File exceeds the 25 MB limit",
            },
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


# ---------------------------------------------------------------------------
# POST /upload-material — downloadable course resources (pdf/doc/docx/zip/images)
# ---------------------------------------------------------------------------


@router.post("/upload-material", response_model=UploadValidationResponse)
async def upload_material(
    file: UploadFile = File(...),
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
):
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={"error": "EMPTY_FILE", "message": "Uploaded file is empty"},
        )
    if len(content) > MAX_MATERIAL_SIZE:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "FILE_TOO_LARGE",
                "message": "Material exceeds the 20 MB limit",
            },
        )

    declared_mime = file.content_type or ""
    if declared_mime not in ALLOWED_MATERIAL_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "UNSUPPORTED_MEDIA_TYPE",
                "message": f"MIME type '{declared_mime}' is not allowed. Allowed: pdf, doc, docx, zip, png, jpeg",
            },
        )

    detected_mime = magic.from_buffer(content, mime=True)
    if detected_mime not in DETECTED_MATERIAL_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "MIME_MISMATCH",
                "message": f"File signature indicates '{detected_mime}', which does not match declared type",
            },
        )

    is_image = detected_mime.startswith("image/")
    public_id, secure_url = upload_asset(
        content,
        file.filename or "material",
        resource_type="image" if is_image else "raw",
    )

    return UploadValidationResponse(
        valid=True,
        filename=file.filename or "",
        size_bytes=len(content),
        # The declared type is the meaningful one for display (libmagic
        # reports docx as a plain zip on some builds).
        mime_type=declared_mime,
        public_id=public_id,
        secure_url=secure_url,
        message="Material uploaded successfully",
    )


# ---------------------------------------------------------------------------
# DELETE /assets — remove an uploaded content asset from Cloudinary
# ---------------------------------------------------------------------------


@router.delete("/assets", status_code=204)
async def delete_uploaded_asset(
    public_id: str = Query(..., min_length=1),
    payload: dict = Depends(require_role(_TEACHER_ADMIN)),
):
    if not public_id.startswith(DELETABLE_PREFIX) or ".." in public_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "ASSET_NOT_DELETABLE",
                "message": "Only uploaded content assets can be deleted",
            },
        )
    # Already-gone assets are not an error: the caller just wants it absent.
    delete_asset(public_id)


# ---------------------------------------------------------------------------
# POST /avatar — self-service profile photo upload (any authenticated role)
# ---------------------------------------------------------------------------


@router.post("/avatar", response_model=UploadValidationResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    payload: dict = Depends(require_role(_ALL_ROLES)),
):
    """
    Uploads a profile photo to Cloudinary for the current user. Unlike
    /upload (course materials, teacher/admin only), any authenticated user
    may upload their own avatar — it is not course content.
    """
    content = await file.read()

    if len(content) > MAX_AVATAR_SIZE:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "FILE_TOO_LARGE",
                "message": "Avatar exceeds the 5 MB limit",
            },
        )

    declared_mime = file.content_type or ""
    if declared_mime not in ALLOWED_AVATAR_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "UNSUPPORTED_MEDIA_TYPE",
                "message": f"MIME type '{declared_mime}' is not allowed. Allowed: png, jpeg",
            },
        )

    detected_mime = magic.from_buffer(content, mime=True)
    if detected_mime not in ALLOWED_AVATAR_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "MIME_MISMATCH",
                "message": f"File signature indicates '{detected_mime}', which does not match declared type",
            },
        )

    public_id, secure_url = upload_asset(
        content, file.filename or "avatar", folder=AVATAR_FOLDER
    )

    return UploadValidationResponse(
        valid=True,
        filename=file.filename or "",
        size_bytes=len(content),
        mime_type=detected_mime,
        public_id=public_id,
        secure_url=secure_url,
        message="Avatar uploaded successfully",
    )

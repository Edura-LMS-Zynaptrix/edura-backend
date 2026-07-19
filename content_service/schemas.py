from pydantic import BaseModel


class SignedUrlRequest(BaseModel):
    public_id: str
    course_id: int


class SignedUrlResponse(BaseModel):
    signed_url: str
    expires_at: int


class StreamResponse(BaseModel):
    embed_url: str
    allow_origin: str


class UploadValidationResponse(BaseModel):
    valid: bool
    filename: str
    size_bytes: int
    mime_type: str
    public_id: str
    secure_url: str
    message: str

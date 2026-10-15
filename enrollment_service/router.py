import os
from typing import Optional

import jwt
from database import get_db
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from models import Enrollment
from schemas import EnrollmentResponse
from sqlalchemy.orm import Session

router = APIRouter()
SECRET_KEY = os.getenv("SECRET_KEY", "edura_super_secret_jwt_key_2026_dev")
ALGORITHM = "HS256"


@router.get(
    "/",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
@router.get(
    "/api/enrollments",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_200_OK,
)
@router.get(
    "/enrollments",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
def get_enrollment(
    student_id: Optional[int] = Query(None, description="ID of the student"),
    course_id: Optional[int] = Query(None, description="ID of the course"),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    """
    Query enrollment status by student_id and course_id, or via Bearer token.
    Exposed so other services (e.g., content_service) and students can verify access.
    """
    sid = student_id
    if sid is None and authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        try:
            payload = jwt.decode(
                token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_aud": False}
            )
            sid = int(payload.get("sub", 0))
        except Exception:
            pass

    if sid is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="student_id is required or must be present in Bearer token",
        )

    q = db.query(Enrollment).filter(Enrollment.student_id == sid)
    if course_id is not None:
        q = q.filter(Enrollment.course_id == course_id)

    enrollment = q.order_by(Enrollment.id.desc()).first()
    if not enrollment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Enrollment record not found for student and course",
        )
    return EnrollmentResponse.from_orm_model(enrollment)


@router.delete(
    "/",
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
@router.delete(
    "/enrollments",
    status_code=status.HTTP_200_OK,
)
def delete_enrollment(
    student_id: int = Query(..., description="Student ID to delete enrollments for"),
    course_id: Optional[int] = Query(None, description="Optional Course ID"),
    db: Session = Depends(get_db),
):
    q = db.query(Enrollment).filter(Enrollment.student_id == student_id)
    if course_id is not None:
        q = q.filter(Enrollment.course_id == course_id)
    records = q.all()
    count = len(records)
    for r in records:
        db.delete(r)
    db.commit()
    return {"message": f"Deleted {count} enrollment records"}

from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, status
from models import Enrollment
from schemas import EnrollmentResponse
from sqlalchemy.orm import Session

router = APIRouter()


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
    student_id: int = Query(..., description="ID of the student"),
    course_id: int = Query(..., description="ID of the course"),
    db: Session = Depends(get_db),
):
    """
    Query enrollment status by student_id and course_id.
    Exposed so other services (e.g., content_service) can verify student access.
    """
    enrollment = (
        db.query(Enrollment)
        .filter(
            Enrollment.student_id == student_id,
            Enrollment.course_id == course_id,
        )
        .first()
    )
    if not enrollment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Enrollment record not found for student and course",
        )
    return EnrollmentResponse.from_orm_model(enrollment)

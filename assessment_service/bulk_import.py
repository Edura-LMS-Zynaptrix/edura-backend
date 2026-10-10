"""
CSV / ZIP authoring helpers shared by the question CSV import, the question
image ZIP upload and the bulk exam import (the reference platform's formats).
"""

import csv
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone

from content_client import upload_image
from models import Assessment, AssessmentType, Question, QuestionType
from sqlalchemy import func
from sqlalchemy.orm import Session

# {question_number}-{n}.ext — n = 0 for the question image, 1-5 for options A-E.
IMAGE_NAME = re.compile(r"^(\d+)-(\d+)\.(png|jpe?g|gif|webp)$", re.IGNORECASE)
MAX_ZIP_ENTRIES = 2000
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
SL_TIME = timezone(timedelta(hours=5, minutes=30))


def import_question_rows(db: Session, assessment_id: int, text_content: str) -> tuple[int, list[str]]:
    """Append MCQ rows (questions.csv shape) to an exam. Returns (created, row errors). Caller commits."""
    next_position = (
        db.query(func.coalesce(func.max(Question.position), 0))
        .filter(Question.assessment_id == assessment_id)
        .scalar()
        or 0
    )
    created = 0
    errors: list[str] = []
    for row_number, row in enumerate(csv.DictReader(io.StringIO(text_content)), start=2):
        def cell(name: str) -> str:
            return (row.get(name) or "").strip()

        question_text = cell("question_text")
        filled = [(letter, cell(f"option_{letter.lower()}")) for letter in "ABCDE"]
        filled = [(letter, text) for letter, text in filled if text]
        if not question_text:
            errors.append(f"Row {row_number}: question_text is empty")
            continue
        if len(filled) < 2:
            errors.append(f"Row {row_number}: at least option_a and option_b are required")
            continue
        answer = cell("correct_answer").upper()
        valid = {letter for letter, _ in filled}
        if answer not in valid:
            errors.append(f"Row {row_number}: correct_answer must be one of {', '.join(sorted(valid))}")
            continue
        option_texts = [text for _, text in filled]
        if len(set(option_texts)) != len(option_texts):
            # Answers are matched by option text, so duplicates are ambiguous.
            errors.append(f"Row {row_number}: options must be distinct")
            continue
        next_position += 1
        created += 1
        db.add(
            Question(
                assessment_id=assessment_id,
                question_text=question_text,
                question_type=QuestionType.mcq,
                options_json=json.dumps(option_texts),
                correct_answer=dict(filled)[answer],
                explanation=cell("explanation") or None,
                marks=1,
                position=next_position,
            )
        )
    return created, errors


def safe_zip(data: bytes) -> zipfile.ZipFile:
    """Open an upload as a ZIP, refusing archives that would expand to an unreasonable size."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("The file is not a valid ZIP archive")
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValueError(f"The ZIP has more than {MAX_ZIP_ENTRIES} files")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("The ZIP expands to more than 300 MB")
    return zf


def _basename(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1]


async def apply_question_images(
    db: Session, assessment_id: int, files: list[tuple[str, bytes]]
) -> tuple[int, int, list[str]]:
    """
    Attach images to an exam's questions by filename. Question N is the Nth
    question in display order; option n is the nth option shown (1 = A).
    Returns (applied, skipped, errors). Caller commits.
    """
    questions = (
        db.query(Question)
        .filter(Question.assessment_id == assessment_id)
        .order_by(Question.position, Question.id)
        .all()
    )
    applied, skipped, errors = 0, 0, []
    for name, data in sorted(files):
        base = _basename(name)
        match = IMAGE_NAME.match(base)
        if not match:
            skipped += 1
            continue
        q_num, slot = int(match.group(1)), int(match.group(2))
        if not 1 <= q_num <= len(questions):
            errors.append(f"{base}: there is no question {q_num}")
            continue
        question = questions[q_num - 1]
        options = json.loads(question.options_json or "[]")
        if slot > len(options):
            errors.append(f"{base}: question {q_num} has no option {slot}")
            continue
        try:
            url, public_id = await upload_image(data, base)
        except Exception as e:
            errors.append(f"{base}: {e}")
            continue
        if slot == 0:
            question.question_image_url = url
            question.question_image_public_id = public_id
        else:
            images = json.loads(question.option_images_json or "[]")
            images = (images + [None] * len(options))[: len(options)]
            images[slot - 1] = {"url": url, "public_id": public_id}
            question.option_images_json = json.dumps(images)
        applied += 1
    return applied, skipped, errors


def _parse_bool(raw: str, default: bool) -> bool:
    raw = (raw or "").strip().lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "y")


def _parse_start(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            # Times in the sheet are local (Sri Lanka).
            return datetime.strptime(raw, fmt).replace(tzinfo=SL_TIME)
        except ValueError:
            continue
    raise ValueError(f"scheduled_start '{raw}' must look like 2026-08-01 09:00")


async def bulk_import(db: Session, course_id: int, module_id: int | None, data: bytes) -> dict:
    """exams.csv + thumbnails/ + questions/<folder>/questions.csv + questions/<folder>/images/."""
    zf = safe_zip(data)
    files = {info.filename.replace("\\", "/").lstrip("/"): info for info in zf.infolist() if not info.is_dir()}
    # Allow the bundle to sit inside one top-level folder.
    exams_key = next((k for k in files if _basename(k) == "exams.csv" and k.count("/") <= 1), None)
    if exams_key is None:
        raise ValueError("exams.csv was not found in the ZIP")
    root = exams_key[: -len("exams.csv")]

    def read(path: str) -> bytes | None:
        info = files.get(root + path)
        return zf.read(info) if info else None

    next_position = (
        db.query(func.coalesce(func.max(Assessment.position), 0))
        .filter(Assessment.course_id == course_id, Assessment.module_id == module_id)
        .scalar()
        or 0
    )
    details, errors = [], []
    created_exams = total_questions = skipped = 0
    rows = list(csv.DictReader(io.StringIO(read("exams.csv").decode("utf-8-sig"))))
    for row in rows:
        def cell(name: str) -> str:
            return (row.get(name) or "").strip()

        folder, title = cell("folder"), cell("title")
        label = title or folder or "(unnamed)"
        if not folder or not title:
            skipped += 1
            errors.append({"exam": label, "message": "folder and title are required"})
            continue
        questions_csv = read(f"questions/{folder}/questions.csv")
        if questions_csv is None:
            skipped += 1
            errors.append({"exam": label, "message": f"questions/{folder}/questions.csv is missing"})
            continue
        try:
            starts = _parse_start(cell("scheduled_start"))
            duration = int(cell("duration_minutes") or 60)
            order = int(cell("order_number") or 0) or None
        except ValueError as e:
            skipped += 1
            errors.append({"exam": label, "message": str(e)})
            continue

        warnings: list[str] = []
        image_url = image_public_id = None
        thumb = cell("thumbnail")
        if thumb:
            thumb_bytes = read(f"thumbnails/{thumb}")
            if thumb_bytes is None:
                warnings.append(f"thumbnail {thumb} not found")
            else:
                try:
                    image_url, image_public_id = await upload_image(thumb_bytes, thumb)
                except Exception as e:
                    warnings.append(f"thumbnail {thumb}: {e}")

        next_position += 1
        exam = Assessment(
            course_id=course_id,
            module_id=module_id,
            title=title,
            description=cell("description") or None,
            assessment_type=AssessmentType.exam,
            time_limit_minutes=duration,
            is_published=_parse_bool(cell("is_published"), True),
            scheduled_start=starts,
            position=order or next_position,
            image_url=image_url,
            image_public_id=image_public_id,
        )
        db.add(exam)
        db.flush()

        created, row_errors = import_question_rows(db, exam.id, questions_csv.decode("utf-8-sig"))
        db.flush()
        warnings.extend(row_errors)
        prefix = f"{root}questions/{folder}/images/"
        images = [(k, zf.read(info)) for k, info in files.items() if k.startswith(prefix)]
        applied, _, image_errors = await apply_question_images(db, exam.id, images)
        warnings.extend(image_errors)

        db.commit()
        created_exams += 1
        total_questions += created
        details.append({
            "folder": folder,
            "title": title,
            "exam_id": str(exam.id),
            "questions_created": created,
            "images_applied": applied,
            "warnings": warnings or None,
        })

    return {
        "created_exams": created_exams,
        "total_questions_created": total_questions,
        "skipped_exams": skipped,
        "details": details,
        "errors": errors,
    }


def template_zip() -> bytes:
    buf = io.BytesIO()
    q_csv = (
        "question_text,option_a,option_b,option_c,option_d,option_e,correct_answer,explanation\n"
        "What is 2 + 2?,2,3,4,5,,C,Basic arithmetic\n"
        "Which planet is closest to the Sun?,Venus,Mercury,Earth,Mars,,B,Mercury is the closest planet\n"
    )
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "exams.csv",
            "folder,title,description,duration_minutes,order_number,is_published,scheduled_start,thumbnail\n"
            "exam1,Paper 01 - Introduction,First exam paper,60,1,true,,exam1.jpg\n"
            "exam2,Paper 02 - Advanced Topics,Second exam paper,90,2,true,2026-12-01 09:00,\n",
        )
        zf.writestr("questions/exam1/questions.csv", q_csv)
        zf.writestr("questions/exam2/questions.csv", q_csv)
        zf.writestr("thumbnails/README.txt", "Put exam thumbnails here, named as in the exams.csv 'thumbnail' column.\n")
        zf.writestr(
            "questions/exam1/images/README.txt",
            "Name images {question_number}-{n}.png: n = 0 for the question image, 1 for option A, 2 for B ...\n",
        )
        zf.writestr(
            "README.md",
            "# Edura bulk exam import\n\n"
            "- exams.csv: one row per exam (folder and title required; times are local, e.g. 2026-12-01 09:00).\n"
            "- questions/<folder>/questions.csv: the same columns as the single-exam question import.\n"
            "- questions/<folder>/images/: optional images named {question_number}-{n}.png.\n"
            "- thumbnails/: optional exam thumbnails referenced by exams.csv.\n",
        )
    return buf.getvalue()

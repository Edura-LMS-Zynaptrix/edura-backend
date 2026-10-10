"""Monthly progress report PDF, built from the student progress view."""

import io
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SL_TIME = timezone(timedelta(hours=5, minutes=30))
BRAND = colors.HexColor("#0d9488")


def now_local() -> datetime:
    return datetime.now(SL_TIME)


def _minutes(seconds) -> str:
    return f"{round(seconds / 60, 1)} min" if seconds else "N/A"


def _text(value) -> str:
    """Paragraphs parse markup, so names like "A & B" must be escaped."""
    return escape(str(value)) if value not in (None, "") else "N/A"


def _score_trend_chart(scores: list, width=450, height=150) -> Drawing:
    drawing = Drawing(width, height)
    plot = LinePlot()
    plot.x, plot.y = 30, 30
    plot.height, plot.width = height - 50, width - 60
    plot.data = [tuple((i + 1, score) for i, score in enumerate(scores))]
    plot.lines[0].strokeColor = BRAND
    plot.lines[0].strokeWidth = 2
    plot.lines[0].symbol = makeMarker("FilledCircle", size=3)
    plot.xValueAxis.valueMin = 0
    plot.xValueAxis.valueMax = max(len(scores), 5)
    plot.xValueAxis.labels.fontSize = 8
    plot.yValueAxis.valueMin = 0
    plot.yValueAxis.valueMax = 100
    plot.yValueAxis.valueStep = 20
    plot.yValueAxis.labels.fontSize = 8
    drawing.add(plot)
    return drawing


def generate_report_pdf(data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Heading1"], fontSize=26, textColor=BRAND, spaceAfter=10, alignment=1, leading=30
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle", parent=styles["Normal"], fontSize=10, textColor=colors.grey, alignment=1, spaceAfter=30
    )
    section_style = ParagraphStyle(
        "SectionHeader", parent=styles["Heading2"], fontSize=16, textColor=colors.darkslategray, spaceBefore=20, spaceAfter=15
    )
    sub_section_style = ParagraphStyle(
        "SubSectionHeader", parent=styles["Heading3"], fontSize=13, textColor=BRAND, spaceBefore=10, spaceAfter=8
    )
    normal = styles["Normal"]
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=8, textColor=colors.grey)

    student = data.get("student", {})
    overall = data.get("overall", {})
    elements = [
        Paragraph("EDURA PROGRESS INSIGHTS", title_style),
        Paragraph(f"Monthly Diagnostic Report • Generated {now_local().strftime('%B %d, %Y')}", subtitle_style),
    ]

    profile = Table(
        [
            [Paragraph(f"<b>Student:</b> {_text(student.get('full_name'))}", normal),
             Paragraph(f"<b>Grade:</b> {_text(student.get('grade'))}", normal)],
            [Paragraph(f"<b>School:</b> {_text(student.get('school'))}", normal),
             Paragraph(f"<b>District:</b> {_text(student.get('district'))}", normal)],
        ],
        colWidths=[3 * inch, 3 * inch],
    )
    profile.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.lightgrey),
    ]))
    elements += [profile, Spacer(1, 20), Paragraph("Executive Performance Metrics", section_style)]

    summary = Table(
        [
            ["Completion Rate", f"{overall.get('completion_pct', 0)}%",
             f"{overall.get('unique_exams_attempted', 0)} / {overall.get('total_unique_exams', 0)} Exams"],
            ["Average Accuracy", f"{overall.get('avg_score_pct', 0)}%", "Across all attempts"],
            ["Total Practice Volume", str(overall.get("total_attempts_made", 0)), "Total attempts submitted"],
            ["Avg Time per Exam", _minutes(overall.get("avg_time_seconds")), "Efficiency marker"],
        ],
        colWidths=[2 * inch, 1.5 * inch, 2.5 * inch],
    )
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
        ("TEXTCOLOR", (1, 0), (1, -1), BRAND),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("PADDING", (0, 0), (-1, -1), 10),
    ]))
    elements += [summary, Paragraph("Deep Subject Analysis", section_style)]

    if not data.get("subjects"):
        elements.append(Paragraph("No exams are available to this student yet.", normal))

    for subject in data.get("subjects", []):
        elements.append(Paragraph(_text(subject["subject"]), sub_section_style))
        stats = Table(
            [
                ["Best Score", "Avg Score", "Efficiency"],
                [f"{subject['best_score_pct']}%", f"{subject['avg_score_pct']}%",
                 f"{_minutes(subject['avg_time_seconds'])}/avg"],
            ],
            colWidths=[2 * inch, 2 * inch, 2 * inch],
        )
        stats.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 1), (0, 1), colors.HexColor("#10b981")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        elements.append(stats)

        if len(subject.get("score_trend") or []) > 1:
            elements.append(Paragraph("Performance Trend (Recent Attempts)", small))
            elements.append(_score_trend_chart(subject["score_trend"]))

        attempted = [e for e in subject["exams"] if e["history"]]
        if not attempted:
            elements.append(Paragraph("No attempts in this subject yet.", small))
            elements.append(Spacer(1, 20))
            continue
        # Five most recently attempted exams; history[0] is each exam's latest attempt.
        attempted.sort(key=lambda e: e["history"][0].get("submitted_at") or "", reverse=True)
        rows = [["Exam Title", "Score", "Time", "Date"]]
        for exam in attempted[:5]:
            latest = exam["history"][0]
            title = exam["title"]
            rows.append([
                title[:35] + "..." if len(title) > 35 else title,
                f"{exam['latest_score_pct']}%",
                _minutes(latest.get("time_seconds")),
                (latest.get("submitted_at") or "N/A")[:10],
            ])
        history = Table(rows, colWidths=[3 * inch, 1 * inch, 1 * inch, 1 * inch])
        history.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("GRID", (0, 0), (-1, -1), 0.2, colors.grey),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ]))
        elements += [Paragraph("Recent Attempt History", small), history, Spacer(1, 20)]

    elements += [
        Spacer(1, 40),
        Paragraph("<b>End of Diagnostic Report</b>", subtitle_style),
        Paragraph("Keep practicing! Consistency is the path to mastery.", subtitle_style),
    ]
    doc.build(elements)
    return buffer.getvalue()

"""Exam leaderboards: each student's FIRST graded attempt, higher score first, faster time breaking ties.

Retakes never move a student up the board, so the ranking reflects how they did
when they first sat the exam.
"""


def rank_rows(rows: list[dict]) -> list[dict]:
    """rows: {"score", "time_seconds", "district", ...}. Adds "rank" and "district_rank" (tied rows share a rank)."""
    ordered = sorted(rows, key=lambda r: (-r["score"], r["time_seconds"]))
    previous_key, previous_rank = None, 0
    district_seen: dict = {}
    for position, row in enumerate(ordered, start=1):
        key = (row["score"], row["time_seconds"])
        row["rank"] = previous_rank if key == previous_key else position
        previous_key, previous_rank = key, row["rank"]

        count, last_key, last_rank = district_seen.get(row["district"], (0, None, 0))
        count += 1
        row["district_rank"] = last_rank if key == last_key else count
        district_seen[row["district"]] = (count, key, row["district_rank"])
    return ordered

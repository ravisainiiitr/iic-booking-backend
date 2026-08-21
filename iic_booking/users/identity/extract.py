"""Parse Channel-I / Omniport academic identity facts without inventing fields."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def normalize_label(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(str(value).strip().split()).casefold()


def _omniport_dict_get(data: dict, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _parse_omniport_date(value):
    if value is None:
        return None
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day") and not isinstance(value, str):
        try:
            return value.date() if hasattr(value, "hour") else value
        except Exception:
            return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    for date_format in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def _role_start_end_dates(role: dict) -> tuple:
    start_raw = _omniport_dict_get(
        role,
        "start_date",
        "startDate",
        "start date",
        "joining_date",
        "joiningDate",
        "joining date",
    )
    end_raw = _omniport_dict_get(
        role,
        "end_date",
        "endDate",
        "end date",
        "graduation_date",
        "graduationDate",
        "graduation date",
    )
    return _parse_omniport_date(start_raw), _parse_omniport_date(end_raw)


def _nested(data: Any, *path: str) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        name = value.get("name") or value.get("fullName") or ""
        return str(name).strip()
    return str(value).strip()


def extract_channel_i_academic_facts(user_info: dict) -> dict[str, Any]:
    """Extract identity facts from the live Channel-I userinfo dict.

    Supports both nested (student.branch.degree.name) and already-parsed flat keys
    used by the existing Omniport callback.
    """
    student = user_info.get("student") or user_info.get("student_member") or {}
    if not isinstance(student, dict):
        student = {}
    faculty = user_info.get("facultyMember") or user_info.get("faculty_member") or {}
    if not isinstance(faculty, dict):
        faculty = {}

    has_student = bool(student)
    has_faculty = bool(faculty)

    nested_degree = _text(_nested(student, "branch", "degree", "name") or _nested(student, "branch", "degree"))
    nested_dept = _text(
        _nested(student, "branch", "department", "name") or _nested(student, "branch", "department")
    )
    nested_branch = _text(_nested(student, "branch", "name") or _nested(student, "branch"))

    flat_degree = _text(
        student.get("branch degree name") or student.get("branch_degree_name") or student.get("degree_name")
    )
    flat_dept = _text(
        student.get("branch department name")
        or student.get("branch_department_name")
        or student.get("department_name")
    )
    flat_branch = _text(student.get("branch name") or student.get("branch_name"))

    degree_name = nested_degree or flat_degree
    department_name = nested_dept or flat_dept
    branch_name = nested_branch or flat_branch

    start_date, end_date = _role_start_end_dates(student) if student else (None, None)
    nested_start = _parse_omniport_date(_nested(student, "start_date") or _nested(student, "startDate"))
    nested_end = _parse_omniport_date(_nested(student, "end_date") or _nested(student, "endDate"))
    start_date = start_date or nested_start
    end_date = end_date or nested_end

    faculty_dept = _text(faculty.get("department name") or faculty.get("department_name") or "")
    faculty_designation = _text(faculty.get("designation") or "")

    username = _text(
        user_info.get("username") or user_info.get("preferred_username") or user_info.get("preferredUsername")
    )
    user_id = _text(user_info.get("userId") or user_info.get("user_id") or user_info.get("sub"))
    enrolment = _text(
        student.get("enrolmentNumber")
        or student.get("enrolment_number")
        or student.get("enrollmentNumber")
        or ""
    )
    from iic_booking.users.identity.gender import extract_channel_i_sex, normalize_channel_i_sex

    channel_i_sex = extract_channel_i_sex(user_info)
    gender = normalize_channel_i_sex(channel_i_sex)

    return {
        "channel_i_user_id": user_id,
        "channel_i_username": username,
        "student_enrolment_number": enrolment,
        "student_degree_name": degree_name,
        "student_department_name": department_name,
        "student_branch_name": branch_name,
        "student_start_date": start_date,
        "student_end_date": end_date,
        "faculty_department_name": faculty_dept,
        "faculty_designation": faculty_designation,
        "channel_i_sex": channel_i_sex,
        "normalized_gender": gender or "",
        "has_student_payload": has_student,
        "has_faculty_payload": has_faculty,
        "raw_student_keys": sorted(student.keys()) if student else [],
    }


def facts_as_history_values(facts: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in (
        "channel_i_user_id",
        "channel_i_username",
        "student_enrolment_number",
        "student_degree_name",
        "student_department_name",
        "student_start_date",
        "student_end_date",
        "faculty_department_name",
        "faculty_designation",
        "channel_i_sex",
        "normalized_gender",
    ):
        val = facts.get(key)
        if isinstance(val, date):
            out[key] = val.isoformat()
        else:
            out[key] = str(val or "")
    return out

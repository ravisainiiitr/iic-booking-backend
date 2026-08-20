# AI.31 — Identity mapping specification

**Status:** CONFIRMED from live Channel-I `get_user_data` on production  
**Date:** 2026-08-20  
**Confirmed by:** Portal operator (after staff + student login captures)

## Authoritative ID rules (migration)

| User kind | Channel-I field | Meaning | Use for migration / wallet key |
|-----------|-----------------|---------|--------------------------------|
| **Staff / non-student** | `username` | Institutional **Employee ID** | Yes — primary Employee ID |
| **Student** | `student.enrolmentNumber` | **Enrolment / Student ID** | Yes — primary student identity key |
| **Student** | `username` | Often equals enrolment | Supporting; usually same as `enrolmentNumber` |

### Live confirmation examples

| Account | `username` | `student.enrolmentNumber` | Confirmed ID |
|---------|------------|---------------------------|--------------|
| Staff | `100673` | *(empty `student`)* | Employee ID = **`100673`** (`username`) |
| Student | `24905001` | `24905001` | Student ID = **`24905001`** (`enrolmentNumber` / `username`) |

## Full Channel-I → portal mapping

| Channel-I field | Semantic | Django / migration use |
|-----------------|----------|------------------------|
| `userId` | Channel-I subject | `User.internal_id` |
| `username` | Employee ID (staff) or enrolment (student) | Staff → `User.emp_id`; Student → same as enrolment when equal |
| `student.enrolmentNumber` | Student / enrolment ID | Student migration key; prefer over inventing IDs |
| `person.fullName` | Display name | `User.name` |
| `person.displayPicture` | Photo path | Profile picture (first seed only) |
| `student.branch degree name` | Degree | Classification (UG/PG/PhD) |
| `student.branch department name` | Department | Dept mapping / HoD |
| `student.branch name` | Programme / branch | Academic profile |
| `student.startDate` / `endDate` | Validity window | Student lifecycle |
| `student.currentYear` / `currentSemester` | Progress | Optional UI |
| `facultyMember.*` | Faculty block | Use when present (often `{}` for staff) |
| `contactInformation.instituteWebmailAddress` | Institute email | `User.email` (login key) |
| `contactInformation.primaryPhoneNumber` | Phone | `User.phone_number` |
| `biologicalInformation.dateOfBirth` | DOB | Optional |
| `roles` | Role list | **Not present** in live payloads — do not depend on it |

## Provenance for wallet linking

Eligible after this confirmation:

- `CHANNEL_I_VERIFIED` — Employee ID from `username` (staff) or enrolment from `student.enrolmentNumber` (student)

Still require exact match to legacy wallet Employee ID / enrolment column before any financial write.

## Existing user policy (unchanged)

- Non-empty `emp_id`: never overwrite from Channel-I.
- Empty `emp_id`: may fill from confirmed rule above if unique.
- Duplicate / conflict: keep existing; mark CONFLICT.
- IIC operator codes are never written as new institutional Employee IDs.

## New user policy

- `internal_id` ← `userId`
- Staff: `emp_id` ← `username`
- Student: store enrolment as institutional student key (`enrolmentNumber`; `username` when identical)
- Email ← `instituteWebmailAddress`

## Notes

1. Live staff payloads often have empty `facultyMember` — do not require faculty claims for Employee ID.
2. Live payloads have **no `roles` key**.
3. Nested student keys use spaces: `branch name`, `branch degree name`, `branch department name`.

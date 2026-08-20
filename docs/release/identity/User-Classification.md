# User Classification

Normalized classifications (from `UserIdentityService.classify_user`):

- `FACULTY` — portal `user_type=faculty` without active HoD assignment
- `HEAD_OF_DEPARTMENT` — active local HoD assignment
- `UNDERGRADUATE_STUDENT` — student + classified undergraduate degree (admin table)
- `OTHER_STUDENT` — student + classified non-UG degree
- `STAFF` — admin / dept_admin / finance / manager / operator / …
- `UNKNOWN` — missing portal type, or unclassified Channel-I degree

Undergraduate degrees are **not** hardcoded to B.Tech. Use `StudentDegreeClassification`.
Unknown degrees do **not** receive undergraduate privileges.

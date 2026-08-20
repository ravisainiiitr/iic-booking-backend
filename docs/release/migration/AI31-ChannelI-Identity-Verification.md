# AI.31 — Channel-I identity verification

**Wallet migration:** not executed. **Financial writes:** none.  
**Live capture:** 2026-08-20 on production (`equip.iitr.ac.in`) after Channel-I OAuth restored.

## Call flow (VERIFIED FROM CODE + LIVE)

```
Browser
  → GET /api/auth/omniport/authorize/
  → https://channeli.in/oauth/authorise/
  → GET /api/auth/omniport/callback/?code=…&state=…
  → POST https://channeli.in/open_auth/token/
  → GET  https://channeli.in/open_auth/get_user_data/
  → Django user create/update + Token login
```

## Live Channel-I response — CAPTURED

Two interactive logins produced `User info received` payloads on production django logs.

### Staff (operator-confirmed Employee ID = `username`)

```json
{
  "userId": 9656,
  "username": "100673",
  "person": { "fullName": "…", "displayPicture": null },
  "student": {},
  "facultyMember": {},
  "biologicalInformation": { "dateOfBirth": "…", "sex": "…", "gender": "…" },
  "contactInformation": {
    "instituteWebmailAddress": "…@iitr.ac.in",
    "primaryPhoneNumber": "…",
    "emailAddress": "…",
    "emailAddressVerified": false
  }
}
```

### Student (operator-confirmed Student ID = `enrolmentNumber`, equals `username`)

```json
{
  "userId": 23740,
  "username": "24905001",
  "person": { "fullName": "…", "displayPicture": null },
  "student": {
    "startDate": "2024-07-18",
    "enrolmentNumber": "24905001",
    "endDate": null,
    "branch name": "Ph.D. Institute Instrumentation",
    "branch degree name": "Ph.D. - Doctor of Philosophy",
    "currentYear": 3,
    "currentSemester": 5,
    "branch department name": "Institute Instrumentation Centre"
  },
  "facultyMember": {},
  "contactInformation": { "instituteWebmailAddress": "…@iitr.ac.in", "…" : "…" }
}
```

## Confirmed ID mapping (operator)

| Role | Authoritative field | Status |
|------|---------------------|--------|
| Staff / non-student | `username` = **Employee ID** | **CONFIRMED** |
| Student | `student.enrolmentNumber` = **Student / enrolment ID** | **CONFIRMED** |
| Student | `username` usually equals enrolment | **CONFIRMED** on live student sample |

See [AI31-Identity-Mapping-Specification.md](AI31-Identity-Mapping-Specification.md).

## User type (LIVE + CODE)

| Signal | Django type |
|--------|-------------|
| non-empty `student` | STUDENT |
| non-empty `facultyMember` | FACULTY |
| both empty | fallback heuristic; existing portal `user_type` is preserved on re-login |

**Live note:** `roles` key was **absent**. Staff can have empty `facultyMember`.

## Wallet gate

Identity claim is confirmed for mapping purposes. Wallet migration still requires exact match to legacy ledger IDs and explicit operator cutover — no bulk financial write from this verification alone.

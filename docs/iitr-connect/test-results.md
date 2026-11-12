# Test results (12 Nov 2026, local)

## Connect suites

| Suite | Result |
|---|---|
| Backend `iic_booking/connect` (M2–M13, 13 files) | **210 passed** (193 feature tests plus 17 M13 security and query-count tests) |
| Frontend `src/pages/connect/Connect.test.tsx` | **31 passed** |

The M13 security tests:
- crawl every Connect route (50+) with every method, unauthenticated, with the flag off, and with an excluding pilot list;
- run 14 reads and 15 writes as Group B members (a member, an external user and a Group B OIC) against Group A objects;
- check that every listing, count, search, suggestion, home section, analytics view, moderation view, notification list and draft leaves Group A out;
- check the private and noindex headers;
- check that query counts stay flat as data grows on the posts, search, home, communities and notifications pages.

## Regression

| Run | Result |
|---|---|
| Frontend, full suite (`npx vitest run`) | **328 files, 2,474 passed**, 1 skipped, 0 failed |
| Backend, broad subset (Connect, facility_groups, communication, users, my_research, and equipment tests matching booking, RBAC, permission, visibility, access, notification, OIC, operator) | 4,369 passed, 30 failed |

The 30 backend failures were compared with a clean checkout of the base branch (`origin/hotfix/sample-label-courier-289` at `172f8303`, no Connect):

| Failures | Status on the base branch | Conclusion |
|---|---|---|
| 14 users tests (`test_supervisor_invites` ×5, `test_portal_migration_ledger` ×2, `test_phase8b` ×2, `test_phase8c` ×3, `test_omniport_sync_savepoint`, `test_phase10e`) | Fail on base too (16 failed there; 2 more are flaky) | Pre-existing |
| 14 equipment and my_research tests (3D-print estimate, orientation, size and support ×8, tinkering-lab cleanup, hour-formula fields ×2, fuzzy global search, research-group reminders ×3) | Fail on base too | Pre-existing |
| `test_booking_status_override::test_not_utilized_to_completed…` and `test_global_search::test_oic_sees_bookings…` | Pass on base | **Pass in the Connect worktree on a fresh test DB** (both files, 26/26). The failures came from the reused test DB losing its email templates and keeping leftover rows. |

**No regression is attributable to Connect.** Booking, payment, wallet and RBAC code is unchanged. The only changes outside the new app:
- `config/settings/base.py`: flags, throttles, the app entry;
- `config/api_router.py`: one include;
- `communication/notification_preferences.py`: one category.

## Manual checks (in-app browser, local backend with the flag on and demo data)

- Landing, community, thread, search, Knowledge Hub, equipment Discussions tab, phone layout.
- Another group's member opening a thread link gets a neutral "not available" page (the API answers 404 for the thread and the community).
- Accept as the asker → Resolved banner; promote → review by the OIC (not the author) → publish → "Reviewed institutional guidance".
- Signed out, `/equipment/<id>` (public QR page) shows no Discussions, no thread text and no "IITR Connect", and makes no Connect API request.

# Files and migrations

Generated from git (diff from each branch's base). Branch `feat/iitr-connect` in both repositories, local commits only.

## Backend (`D:\IIC_NEW\wt-be-connect`, base `172f8303`)

### Commits

- `0cff916a` IITR Connect M2: models, migrations, access rules and the feature flag (off by default)
- `6e1e1b21` IITR Connect M3: one community per Equipment Group, membership sync from bookings and staff
- `792e229f` IITR Connect M4: threads, replies, categories, tags, reactions, polls, drafts and private attachments
- `2aa9eedf` IITR Connect M5 (backend support): landing page sections, @mention suggestions, community follow state
- `1fa2fd4b` IITR Connect M6: permission-aware search and "find a solution before raising a query"
- `722b3aa7` IITR Connect M7: accepted solutions and the Knowledge Hub (promote, review, publish, revisions)
- `41b40aab` IITR Connect M8: staff tools on threads and the equipment page Discussions panel
- `dc71c4e1` IITR Connect M9 + M10: announcements, notifications and digests; "Resolved" by the person who asked
- `aea74968` IITR Connect tests: re-seed the notification email templates per test (a reused test DB can lose migration data)
- `6de6afa8` IITR Connect M11 + M12: moderation, analytics and recognition
- `90dff1bc` IITR Connect M13: security and query-count tests, delivery documentation
- (this file's refresh is the next commit)

### Changed outside the new app

     config/api_router.py                                  |  2 ++
     config/settings/base.py                               | 15 +++++++++++++++
     iic_booking/communication/notification_preferences.py |  6 ++++++

### New app `iic_booking/connect/` and docs

- `docs/iitr-connect/README.md`
- `docs/iitr-connect/api.md`
- `docs/iitr-connect/architecture.md`
- `docs/iitr-connect/er-diagram.md`
- `docs/iitr-connect/files-and-migrations.md`
- `docs/iitr-connect/known-limitations.md`
- `docs/iitr-connect/local-testing.md`
- `docs/iitr-connect/migrations.md`
- `docs/iitr-connect/performance-review.md`
- `docs/iitr-connect/rollout-plan.md`
- `docs/iitr-connect/security-review.md`
- `docs/iitr-connect/test-results.md`
- `docs/iitr-connect/user-guide.md`
- `iic_booking/connect/__init__.py`
- `iic_booking/connect/access.py`
- `iic_booking/connect/admin.py`
- `iic_booking/connect/api/__init__.py`
- `iic_booking/connect/api/admin_views.py`
- `iic_booking/connect/api/base.py`
- `iic_booking/connect/api/communities.py`
- `iic_booking/connect/api/equipment.py`
- `iic_booking/connect/api/home.py`
- `iic_booking/connect/api/knowledge.py`
- `iic_booking/connect/api/moderation.py`
- `iic_booking/connect/api/notifications.py`
- `iic_booking/connect/api/presenters.py`
- `iic_booking/connect/api/search.py`
- `iic_booking/connect/api/threads.py`
- `iic_booking/connect/api/views.py`
- `iic_booking/connect/apps.py`
- `iic_booking/connect/management/__init__.py`
- `iic_booking/connect/management/commands/__init__.py`
- `iic_booking/connect/management/commands/connect_sync_communities.py`
- `iic_booking/connect/migrations/0001_initial.py`
- `iic_booking/connect/migrations/0002_seed_general_categories.py`
- `iic_booking/connect/migrations/0003_nightly_membership_sync_schedule.py`
- `iic_booking/connect/migrations/0004_article_reviewed_version.py`
- `iic_booking/connect/migrations/0005_notification_templates_and_digests.py`
- `iic_booking/connect/migrations/0006_thread_resolved_by_author.py`
- `iic_booking/connect/migrations/__init__.py`
- `iic_booking/connect/models/__init__.py`
- `iic_booking/connect/models/actions.py`
- `iic_booking/connect/models/base.py`
- `iic_booking/connect/models/communities.py`
- `iic_booking/connect/models/knowledge.py`
- `iic_booking/connect/models/moderation.py`
- `iic_booking/connect/models/threads.py`
- `iic_booking/connect/permissions.py`
- `iic_booking/connect/sanitize.py`
- `iic_booking/connect/services/__init__.py`
- `iic_booking/connect/services/analytics.py`
- `iic_booking/connect/services/announcements.py`
- `iic_booking/connect/services/attachments.py`
- `iic_booking/connect/services/knowledge.py`
- `iic_booking/connect/services/membership.py`
- `iic_booking/connect/services/moderation.py`
- `iic_booking/connect/services/notifications.py`
- `iic_booking/connect/services/recognition.py`
- `iic_booking/connect/services/search.py`
- `iic_booking/connect/services/solutions.py`
- `iic_booking/connect/services/staff_tools.py`
- `iic_booking/connect/services/threads.py`
- `iic_booking/connect/signals.py`
- `iic_booking/connect/tasks.py`
- `iic_booking/connect/tests/__init__.py`
- `iic_booking/connect/tests/conftest.py`
- `iic_booking/connect/tests/test_m11_moderation.py`
- `iic_booking/connect/tests/test_m12_analytics_recognition.py`
- `iic_booking/connect/tests/test_m13_security.py`
- `iic_booking/connect/tests/test_m2_access.py`
- `iic_booking/connect/tests/test_m3_membership.py`
- `iic_booking/connect/tests/test_m4_threads.py`
- `iic_booking/connect/tests/test_m5_home.py`
- `iic_booking/connect/tests/test_m6_search.py`
- `iic_booking/connect/tests/test_m7_knowledge.py`
- `iic_booking/connect/tests/test_m8_equipment.py`
- `iic_booking/connect/tests/test_m9_m10_announcements_notifications.py`
- `iic_booking/connect/urls.py`

### Migrations

- `connect/0001_initial.py`
- `connect/0002_seed_general_categories.py`
- `connect/0003_nightly_membership_sync_schedule.py`
- `connect/0004_article_reviewed_version.py`
- `connect/0005_notification_templates_and_digests.py`
- `connect/0006_thread_resolved_by_author.py`

## Frontend (`D:\IIC_NEW\wt-fe-connect`, base `7015afd`)

### Commits

- `a8329f9` IITR Connect M5: landing page, communities, discussion feed, thread page and editor
- `b4088d5` IITR Connect M6: search results with highlights, accepted-answer previews and suggestions while typing
- `c4a4454` IITR Connect M7: accept answers, solved banner, Knowledge Hub list and article workflow
- `53961eb` IITR Connect M8: Discussions tab on the equipment page and staff tools on threads
- `1a59f24` IITR Connect M9 + M10 (frontend): announcements, notifications page, and a clear "Resolved" badge
- `019919b` IITR Connect M11 + M12 (frontend): reporting, moderation console, analytics, staff navigation
- `becd09b` IITR Connect M13: robots.txt disallows /connect and its API (the rest of the site stays crawlable as before)

### Files

     public/robots.txt                                  |    5 +
     src/components/DashboardHeader.tsx                 |   10 +-
     src/components/connect/ConnectEditor.tsx           |  480 +++++++++
     .../connect/EquipmentDiscussionsPanel.tsx          |  191 ++++
     src/components/connect/PostBody.tsx                |  107 ++
     src/components/connect/SimilarDiscussions.tsx      |   90 ++
     src/components/connect/ThreadFeed.tsx              |  392 ++++++++
     src/components/connect/connect.css                 |  112 +++
     src/components/connect/connectUi.tsx               |  471 +++++++++
     src/components/connect/useConnect.ts               |   92 ++
     src/lib/connectApi.ts                              |  619 ++++++++++++
     src/pages/EquipmentProfile.tsx                     |   28 +-
     src/pages/connect/Connect.test.tsx                 |  795 +++++++++++++++
     src/pages/connect/ConnectAnalytics.tsx             |  167 ++++
     src/pages/connect/ConnectArticle.tsx               |  340 +++++++
     src/pages/connect/ConnectCommunities.tsx           |  106 ++
     src/pages/connect/ConnectCommunity.tsx             |  154 +++
     src/pages/connect/ConnectHome.tsx                  |  287 ++++++
     src/pages/connect/ConnectKnowledge.tsx             |  165 ++++
     src/pages/connect/ConnectModeration.tsx            |  297 ++++++
     src/pages/connect/ConnectNewThread.tsx             |  474 +++++++++
     src/pages/connect/ConnectNotifications.tsx         |  151 +++
     src/pages/connect/ConnectSearch.tsx                |   42 +
     src/pages/connect/ConnectThread.tsx                | 1019 ++++++++++++++++++++
     src/routes/AppRoutes.tsx                           |   23 +

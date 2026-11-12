# Architecture

## Principles

- **Reuse, never duplicate.** Users, equipment, Equipment Groups, bookings, roles and departments are the portal's own models. Connect only references them.
- **Private by construction.** One module, `iic_booking/connect/access.py`, decides who may read, post and moderate. Every query starts from its visible querysets. An object the caller may not read answers **404**, never 403, so its existence is not revealed.
- **Off by default.** While `IITR_CONNECT_ENABLED` is off:
  - every endpoint answers 404 (the bootstrap answers `enabled: false`);
  - every signal and task does nothing;
  - the UI hides all entry points.
- **No effect on operations.** Discussions, staff flags, announcements and analytics never read or write bookings, slots, payments, wallets, equipment status or maintenance.

## Reused portal pieces

| Need | Existing piece reused |
|---|---|
| Identity, roles | `users.User` (`user_type`), `is_active`; portal token auth (`TokenAuthenticationWithInactivity`) |
| Internal users | `equipment.results_sharing_service.is_internal_iitr_user`, plus IITR staff user types |
| Groups and equipment | `EquipmentGroup`, `Equipment.equipment_group`, with modes resolved to the base instrument's group |
| OIC | `EquipmentManager`, `EquipmentTemporaryOIC.objects.active()` |
| Lab In-charge / Operator | `EquipmentOperator`, plus the acting operator of an active `EquipmentOperatorCoverage`. In this portal "Lab In-charge" and "Lab Operator" are the same role. |
| Bookers | `Booking.user`, counted with the Facility User Groups rule (every booking except `PENDING_PAYMENT`) |
| Equipment visibility | `api_views.get_visible_equipment_queryset` (visibility groups, test-only, department catalogue visibility) |
| Rich text | nh3 (server) and DOMPurify (client); TipTap editor |
| File checks | `my_research.file_policy` (content sniffing, name cleaning) |
| Storage | Django default storage: the private S3 bucket in production. Files are streamed through Django; no public or signed URL is ever handed out. |
| Notifications | `communication.in_app.notify_in_app` (bell), `CommunicationService.send_email`, portal notification categories (a new `discussions` category) |
| Background work | Celery with `defer_during_peak`; `django_celery_beat` schedules created by migrations |
| Audit | Append-only models (`AppendOnlyModel`), the same pattern as procurement |

## Policy decisions (confirmed by the portal owner, 11 Nov 2026)

1. **Who joins a group community as a booker:** anyone with a booking of the group's equipment except `PENDING_PAYMENT`, the Facility Groups rule. The booker source is sticky: history is kept.
2. **External users** (industry, R&D, institutes, startups, other, organisation admins, faculty of external departments) join the communities of the groups they booked. **General IITR Discussions is internal-only.**
3. **Staff who leave** become *Lapsed*: no access and no badge, but their history is kept. An administrator can re-add them.
4. **Instruments without a group** use General → *General Instrumentation Discussions*.
5. **Resolved:** the person who asked can mark the accepted answer as Resolved. This gives a distinct badge, separate from a moderator-accepted "Solved".

## Components

- **Communities and membership (M3).** `services/membership.py`:
  - an idempotent, transactional sync (`select_for_update` per community; dry run equals the real run rolled back);
  - signals on bookings, assignments, equipment group changes and groups;
  - a nightly full sync;
  - a membership audit log.
- **Threads (M4).** Nine types. Replies and replies-to-post, edit windows with revisions, author deletes, reactions, follows, bookmarks, polls, drafts, tags, categories, private attachments.
- **Search (M6).**
  - PostgreSQL full-text search with weighted vectors and GIN indexes: global search over titles, bodies, visible replies, tags, equipment, communities and authors.
  - "Find a solution first" suggestions with accepted-answer previews.
  - A `SimilarityBackend` interface, so a semantic backend can be added later.
- **Accepted answers and Knowledge Hub (M7).** Author- or moderator-accepted answers. Articles promoted by curators, with inherited community, four-eyes review, versioned guidance label, revisions and archive.
- **Operator tools and equipment panel (M8).**
  - Pin, known problem, workaround, outdated and resolved flags, all audited.
  - A Discussions tab on the signed-in equipment page only. The public QR page is untouched.
- **Announcements (M9).** Priority, expiry, targeting by instrument, acknowledgement, pin.
- **Notifications (M10).**
  - The bell plus email: immediate, daily digest, weekly digest or off.
  - One notification per recipient per post. Access is re-checked at creation and again at email or digest time. Notifications carry titles and links only.
- **Moderation (M11).** Reports with an automatic hide after 3 distinct reporters; hide, remove and restore; lock; move (cross-community moves for admins only, with explicit confirmation); queue, dashboard and audit log; Community Admins appointed by the Portal Admin.
- **Analytics and recognition (M12).**
  - Analytics are scoped to the communities the caller moderates; search insight is admin-only with a minimum count.
  - Optional badges earned from accepted answers, distinct helpful people and approved articles, never from posting volume.

## Booking Assistant (research_copilot)

The Booking Assistant's knowledge articles are portal-wide answers. Connect's Knowledge Hub articles are limited to a community. They are deliberately kept separate: the assistant does not index Connect, and no Connect module is imported anywhere else (checked).

# IITR Connect API

Base path: `/api/v1/connect/`. Every endpoint needs the portal's `Authorization: Token ...` header and answers **404** while `IITR_CONNECT_ENABLED` is off, when a pilot list excludes the caller, or when the caller cannot read the object. The bootstrap is the only exception: it always answers 200 with `enabled`. Responses carry `Cache-Control: private, no-store` and `X-Robots-Tag: noindex`. Write endpoints are rate limited (`CONNECT_*_THROTTLE_RATE`). List endpoints are paginated (`?page=`, `?page_size=` up to 100).

Generated from the OpenAPI schema (`manage.py spectacular`); the full schema is served at `/api/schema/`.

## IITR Connect

| Method | Path | Purpose |
|---|---|---|
| GET | `/bootstrap/` | Connect availability and the caller's capabilities |

## IITR Connect: analytics and settings

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/settings/` | IITR Connect module settings (Portal Admin) |
| PATCH | `/admin/settings/` | Change IITR Connect module settings (Portal Admin; audited) |
| GET | `/analytics/` | Analytics for the communities you moderate (admins: all). Not a downtime measure. |

## IITR Connect: communities

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/sync/` | Recent membership sync runs |
| POST | `/admin/sync/` | Run the membership sync (dry run unless apply=true) |
| GET | `/communities/` | Communities the caller can read (My Communities + General) |
| GET | `/communities/{slug}/` | One community the caller can read (404 otherwise) |
| GET | `/communities/{slug}/audit/` | Membership audit log of one community (admins) |
| GET | `/communities/{slug}/members/` | Members (full list for moderators and admins; staff only for members) |
| POST | `/communities/{slug}/members/{user_id}/` | Suspend, revoke, restore, add or change the moderator role of a member (audited) |

## IITR Connect: discussions

| Method | Path | Purpose |
|---|---|---|
| POST | `/attachments/` | Upload a file for a post (validated, stored privately); bind it by id when posting |
| GET | `/attachments/{id}/` | Download an attachment (access re-checked on every request; never a public URL) |
| DELETE | `/attachments/{id}/` | Remove one of your uploads that is not attached to a post yet |
| GET | `/categories/` | Categories of the readable communities (optionally one community) |
| POST | `/categories/` | Create a category (Portal / Community Admin) |
| PATCH | `/categories/{id}/` | Rename, reorder, deactivate or restrict a category (Portal / Community Admin) |
| PUT | `/communities/{slug}/follow/` | Follow, mute or stop following a whole community |
| GET | `/drafts/` | Your autosaved drafts |
| GET | `/drafts/{key}/` | Get one draft |
| PUT | `/drafts/{key}/` | Autosave a draft (title, body, type, tags, attachments...) |
| DELETE | `/drafts/{key}/` | Discard a draft |
| PATCH | `/posts/{id}/` | Edit a reply (author within the edit window, or a moderator with audit) |
| DELETE | `/posts/{id}/` | Delete your own reply (while it has no replies) |
| POST | `/posts/{id}/react/` | Toggle a reaction (helpful, insightful, thanks) on someone else's post |
| GET | `/posts/{id}/revisions/` | Edit history of a post (author and moderators) |
| GET | `/tags/` | Tags used in threads the caller can read, most used first (?q= prefix) |
| PATCH | `/tags/{slug}/` | Rename or deactivate a tag (Portal / Community Admin) |
| GET | `/threads/` | Discussion feed (filters, sorts, pagination); only threads the caller can read |
| POST | `/threads/` | Start a discussion (announcements also take priority, expires_at, requires_acknowledgement, target_equipment_id, pin) |
| GET | `/threads/{id}/` | A thread with its opening post, poll and the caller's permissions |
| PATCH | `/threads/{id}/` | Edit the thread (title, body, tags, category); author within the edit window or a moderator |
| DELETE | `/threads/{id}/` | Delete your own thread (only while it has no replies) |
| POST | `/threads/{id}/bookmark/` | Toggle bookmark |
| PUT | `/threads/{id}/follow/` | Follow (watching), mute or stop following a thread (level null) |
| GET | `/threads/{id}/posts/` | Replies of a thread (oldest first, 50 per page) |
| POST | `/threads/{id}/posts/` | Reply to the thread or to one of its posts |
| POST | `/threads/{id}/vote/` | Vote in a poll (changing your vote replaces it) |

## IITR Connect: equipment

| Method | Path | Purpose |
|---|---|---|
| GET | `/equipment/{equipment_id}/discussions/` | Discussions panel for an equipment page |
| PATCH | `/threads/{id}/staff/` | Pin, known problem, workaround, outdated, resolved (moderators of the community; audited; never changes equipment) |

## IITR Connect: home

| Method | Path | Purpose |
|---|---|---|
| GET | `/communities/{slug}/mention-candidates/` | @mention suggestions limited to people who can read the community |
| GET | `/home/` | Landing page sections over the caller's readable threads |

## IITR Connect: knowledge

| Method | Path | Purpose |
|---|---|---|
| GET | `/articles/` | Knowledge Hub articles the caller can read (published; drafts for authors and curators) |
| POST | `/articles/` | Promote a discussion (and optionally one reply) to a draft article (curators) |
| GET | `/articles/{id}/` | One article (404 unless the caller can read it) |
| PATCH | `/articles/{id}/` | Edit an article (curators; authors while it is a draft). Creates a revision. |
| GET | `/articles/{id}/revisions/` | Revision history (author and curators) |
| POST | `/articles/{id}/{action}/` | Workflow: submit, review {decision, comment, institutional}, publish, archive, restore |
| POST | `/threads/{id}/solution/` | Accept or replace the solution (thread author or a moderator; audited) |
| PUT | `/threads/{id}/solution/` | The person who asked confirms the accepted answer resolved it (Resolved badge) |
| DELETE | `/threads/{id}/solution/` | Revoke the accepted solution (thread author or a moderator; audited) |

## IITR Connect: moderation

| Method | Path | Purpose |
|---|---|---|
| GET | `/moderation/community-admins/` | Community Admins (Portal Admin only) |
| POST | `/moderation/community-admins/` | Appoint a Community Admin by e-mail (Portal Admin only) |
| DELETE | `/moderation/community-admins/` | Remove a Community Admin (Portal Admin only) |
| GET | `/moderation/dashboard/` | Moderation totals per community you moderate |
| GET | `/moderation/log/` | Moderation audit log (communities you moderate; Portal / Community Admins see all) |
| GET | `/moderation/queue/` | Reported and hidden posts in the communities you moderate |
| POST | `/posts/{id}/moderate/` | Hide, remove or restore a post (moderators; reason required to hide or remove) |
| GET | `/posts/{id}/report/` | Report reasons |
| POST | `/posts/{id}/report/` | Report someone else's post (once per post) |
| POST | `/threads/{id}/lock/` | Lock or unlock a thread (moderators) |
| POST | `/threads/{id}/move/` | Change the category (moderators) or move to another community (Community Admins, confirm_widen) |

## IITR Connect: notifications

| Method | Path | Purpose |
|---|---|---|
| GET | `/notifications/` | Your Connect notifications (only items you can still read are shown) |
| GET | `/notifications/preferences/` | Your Connect notification preferences |
| PUT | `/notifications/preferences/` | Change how and how often Connect notifies you |
| POST | `/notifications/read/` | Mark notifications read ({ids: [...]} or {all: true}) |
| GET | `/threads/{id}/acknowledge/` | Who acknowledged (facility staff and moderators) |
| POST | `/threads/{id}/acknowledge/` | Acknowledge an announcement that asks for it |

## IITR Connect: search

| Method | Path | Purpose |
|---|---|---|
| GET | `/search/` | Search titles, discussion text, replies, equipment, communities, tags and authors (readable threads only) |
| GET | `/suggest/` | Similar readable discussions for a draft title (advisory; never blocks posting) |

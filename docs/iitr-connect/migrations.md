# Migrations

All are in `iic_booking/connect/migrations/`. They are **additive**:
- new tables;
- create-only data (General community, categories, badges, email templates, Beat schedules).

No existing table or row is altered.

| Migration | What it does | Reverse |
|---|---|---|
| `0001_initial` | Creates every `connect_*` table, index and constraint. Depends on `equipment 0262` and the user model. | Drops the Connect tables. |
| `0002_seed_general_categories` | Creates General IITR Discussions, its 8 categories, 5 recognition badges and the settings row (create-only). | No-op; data is kept. |
| `0003_nightly_membership_sync_schedule` | Beat task `connect.nightly_membership_sync`, 02:40 IST. | Deletes the schedule. |
| `0004_article_reviewed_version` | Adds `KnowledgeArticle.reviewed_version`. | Removes the field. |
| `0005_notification_templates_and_digests` | Creates the email templates `connect_activity_email` and `connect_digest_email` only if missing, never overwriting edited templates. Adds Beat tasks for the daily digest (08:05 IST) and weekly digest (Monday 08:10 IST). Depends on `communication 0065`. | Deletes the schedules and any templates nobody edited. |
| `0006_thread_resolved_by_author` | Adds `DiscussionThread.resolved_by_author_at`. | Removes the field. |

## Applying

- Production migrations are run by the portal owner only (Deploy Backend does not migrate).
- After deploying, ask for "run production migrations". The plan shows only `connect 0001–0006`.
- All Beat tasks and signals do nothing until `IITR_CONNECT_ENABLED=true`, so migrating changes no behaviour.
- **Group communities are not created by migrations.** They come from `manage.py connect_sync_communities --apply` (see the rollout plan).

## Rolling back

- **Behaviour:** set `IITR_CONNECT_ENABLED=false`. Everything disappears and no data is lost.
- **Schema:** `manage.py migrate connect zero` drops all Connect data. Use it only on an explicit decision to discard the content.

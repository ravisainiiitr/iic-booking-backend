# Phase 8C — Migration Emails

## Templates

| Code | Audience |
|------|----------|
| `FACULTY_MIGRATION` | Faculty |
| `STUDENT_MIGRATION` | Student / Individual Student |
| `OIC_MIGRATION` | Officer-in-Charge (`manager`) |
| `ADMIN_MIGRATION` | Main Administrator (`admin`) |

Unsupported / ambiguous roles are **reported and skipped** (e.g. Lab-in-Charge, Dept Admin, external types) — never silently misclassified.

## Variables

`{{ user_name }}`, `{{ new_portal_url }}`, `{{ migration_datetime }}`, `{{ support_email }}`, `{{ support_phone }}`, `{{ portal_name }}`

`new_portal_url` comes from `PortalMigrationState.new_portal_url` (not hard-coded).

## Visual

Navy `#1D2844`, institutional table HTML via `wrap_email_html` (Outlook/Gmail safe). No large blocked external images required.

## Batching

Models: `MigrationNotificationBatch`, `MigrationNotificationRecipient`  
Celery: `users.send_migration_notification_recipient`  
T0 creates batch → queues tasks → continues (does not wait on SMTP).

## Safety

```bash
python manage.py migration_notification_dry_run
python manage.py migration_email_preview
```

- Dry-run: ZERO emails
- Idempotent: SENT recipients not resent for same batch
- Production delivery blocked in Phase 8C tooling

## Preview API

`GET /api/portal-migration/admin/email-preview/` (Main Admin) — sample data only.

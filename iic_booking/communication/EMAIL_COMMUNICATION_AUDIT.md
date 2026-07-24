"""
Email Communication Redesign — Audit Report
Date: 2026-07-24
Reference design: Welcome Email (welcome_email.py)

## Summary

All default CommunicationTemplate email rows are redesigned to match the Welcome Email
visual language (teal institutional shell, consistent typography, footer, responsive
table layout). Subject lines are shortened. Template rendering no longer leaves raw
`{{ placeholders }}`. Optional sections hide when empty.

## Templates reviewed (57)

See `DEFAULT_EMAIL_TEMPLATE_CODES` in `default_email_templates.py`.

Includes booking, waitlist, urgent, wallet, registration, support, TA/nomination,
operator leave, OIC report, admin bulk, sample disposed / deadline reminder, and
the previously missing `booking_confirmed_email`.

Welcome Email remains a dedicated builder in `welcome_email.py` (not a DB template).

## Issues found

1. Subjects included booking IDs, and test redirect added `[TEST:email]` (kept for
   test accounts only — production subjects no longer embed booking IDs / emails).
2. Unresolved `{{ var }}` left in body when context keys were missing.
3. Empty Note / Comment / Link sections still rendered (including "No comment").
4. `event.metadata` overwrote `user_name` and other display fields — could show
   numeric user PKs ("Hello 152").
5. Dates used raw `%Y-%m-%d %H:%M:%S`; duration used decimal hours; currency lacked
   consistent thousand separators.
6. Relative or empty booking links appeared in emails.
7. Legacy green/blue Material-style HTML differed from Welcome Email branding.
8. `booking_confirmed_email` referenced in code but never seeded.
9. Styled wallet-join emails used a separate blue shell.

## Fixes implemented

| Area | Change |
|------|--------|
| Design shell | `email_branding.py` — Welcome-aligned layout, footer, CTA, detail cards |
| Defaults catalog | `default_email_templates.py` — 57 branded templates + subjects |
| DB sync | Migration `0050_redesign_all_email_templates.py` + `sync_default_email_templates` |
| Renderer | `{% if var %}…{% endif %}`; missing vars → empty; strip residual braces |
| Booking context | Protected keys; `user_display_name`; formatted date/duration/INR; absolute links only; empty comments |
| Wallet / styled | Same shell; cleaner subjects; display-name helpers |
| Currency/date/duration | Shared formatters applied in context + sanitize_template_context |

## Subject line policy

- User emails: action + equipment (or short wallet/support phrase). No booking ID, user name, or email.
- Exception: SRIC office wallet recharge may include faculty name + emp id.
- Test accounts still get `[TEST:…]` prefix via `redirect_email_for_user` (intentional for QA).

## How to apply

1. Deploy backend and run migrations (`0050_…`).
2. Or: `python manage.py sync_default_email_templates`
3. Optionally re-sync after editing the catalog: same command.
4. Spot-check via Admin → Communication Templates, and `send_test_email`.

## Remaining recommendations

1. Add automated snapshot tests that render every catalog template with sample context
   and assert no `{{` / `{%` remain.
2. Seed missing push notification templates (`booking_*_push`, wallet push) if push
   delivery is required in production.
3. Consider a dedicated `sample_accepted_email` if sample acceptance should not reuse
   status/comment templates.
4. Confirm `SUPPORT_EMAIL` / `FRONTEND_URL` / `ORG_LEGAL_NAME` in production settings
   so footers and CTAs resolve correctly.
5. After deploy, send one live booking confirmation and one wallet email to a test
   mailbox and verify Gmail + Outlook rendering.
"""

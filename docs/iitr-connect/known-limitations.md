# Known limitations and follow-ups

## By design (decided)

- **Lab In-charge and Lab Operator** are one role in this portal (`EquipmentOperator`), so there is one badge: "Lab Operator (Lab In-charge)".
- **Department Administrators** are not automatically members of group communities. They are not bookers or equipment staff. A Portal Admin can add them, or appoint them Community Admins.
- **External users** take part in the communities of groups they booked but never see General IITR Discussions. On an ungrouped instrument (whose threads live in General) they get no Discussions panel.
- **Moving a thread to another community** is admin-only and deliberately widens its readers. Articles keep their original community and are never widened.
- **Search matches only visible text, moderators included.** Hidden content is handled in the moderation queue.

## Not implemented (possible follow-ups)

1. **Header search integration.** The portal's top search (`equipment/global_search.py`) does not include Connect results. This was left out to keep shared code untouched; it is easy to add behind the flag.
2. **Semantic search.** Suggestions use PostgreSQL full-text search. `SimilarityBackend` allows an embedding backend later; none exists today.
3. **Malware scanning of attachments.** Not available on this infrastructure. Uploads are type-checked by content and Office files are always downloaded, never rendered.
4. **Search log retention.** `ConnectSearchLog` keeps normalised queries indefinitely. A retention purge (for example 12 months) should be added before general release.
5. **Emails use the shared portal template shell** created at migration time. Visual QA on real mail clients is still to do.
6. **Poll options are single-choice.** There are no multiple-choice or anonymous polls.
7. **No "edit announcement settings" endpoint after posting.** Staff can edit the text; priority, expiry, target and acknowledgement are set at creation.
8. **No "request to join" for a group.** Membership comes only from bookings, assignments and admins, as specified.
9. **Mobile app (Capacitor).** Connect pages work in the web view through the same routes; there is no native integration or push-specific work.
10. **Real-account checks.** Verified only with tests and local demo accounts. Production data shapes (number of groups, very large General audiences) are untested until the dry-run report.
11. **Two lint warnings** (`react-refresh/only-export-components`) in shared Connect UI files. This is cosmetic.

## Environment notes found while testing (not Connect issues)

- **16 users-app tests fail on the clean base branch too** (`test_supervisor_invites`, `test_portal_migration_ledger`, `test_phase8b/8c`, `test_omniport_sync_savepoint`, `test_phase10e`). They predate Connect.
- **Reused test databases.** A transactional test elsewhere can wipe migration data in a reused test DB (`--reuse-db`). Connect tests re-seed what they need.
- **`python - <<EOF` hangs** in this Windows Git Bash setup. Use script files.

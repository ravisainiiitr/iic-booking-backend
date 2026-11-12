# Safe production rollout

Nothing here has been done. Each step needs the portal owner's explicit go-ahead (push, merge, deploy and production migrations included).

## 0. Bring the branches up to date

- **Backend:** `feat/iitr-connect` was cut from `hotfix/sample-label-courier-289` at `172f8303` (v2.5.47.405). The base has moved on since. A trial merge (`git merge-tree`) shows **one trivial conflict** in `config/settings/base.py`: `LOCAL_APPS` gained `gst_einvoice` where Connect adds `connect`. Keep both lines.
- **Frontend:** `feat/iitr-connect` was cut from `master` at `7015afd`. It merges cleanly with the current `master`.
- After rebasing, re-run the Connect suites (see local-testing.md).

## 1. Release with the module off

1. Push the branches and open PRs (backend into the release branch, frontend into `master`). Review and merge.
2. Deploy the backend and frontend as usual. `IITR_CONNECT_ENABLED` stays **unset or false**.
   - Every Connect endpoint answers 404; the UI shows no entry point.
   - Booking, payment and wallet behaviour is unchanged.
3. Ask for **run production migrations**: `connect 0001–0006`, additive only. The Beat schedules they add do nothing while the module is off.

## 2. Build the communities (still off)

1. On the backend container:
   ```bash
   python manage.py connect_sync_communities --report /tmp/connect-sync.json
   ```
   This is a dry run and writes nothing. Review the counts: communities per Equipment Group, members created, staff, test-only groups.
2. If the counts look right:
   ```bash
   python manage.py connect_sync_communities --apply
   ```
   It is idempotent, so running it again changes nothing.

## 3. Pilot

1. Set:
   - `IITR_CONNECT_ENABLED=true`
   - `IITR_CONNECT_PILOT_EMAILS` to the test accounts and a few staff members (comma-separated)
2. Restart. Only pilot users see Connect; everyone else still gets 404.
3. Pilot checklist:
   - Membership: a member sees their group and not others; an external user sees their group but not General.
   - Equipment page: the Discussions tab works for members and shows "members only" to others.
   - Signed out: the public QR page shows no discussions.
   - Notifications: replies arrive in the bell; emails carry titles and links only.
   - Search: finds threads and replies.
   - Workflows: accept and resolve, then promote → review → publish an article.
   - Moderation queue works for the OIC.

## 4. Widen, then open

- Add more pilot emails; then clear `IITR_CONNECT_PILOT_EMAILS` to open Connect to all eligible users.
- Optionally enable recognition badges under Moderation → Module settings once there is enough activity.

## Rollback

| Situation | Action |
|---|---|
| Any problem | Set `IITR_CONNECT_ENABLED=false` and restart. Everything is hidden at once; no data is lost; booking is unaffected. |
| A noisy notification | Users can switch it off themselves; the admin can mute email by turning the portal "IITR Connect discussions" category off for a user. |
| Discard all Connect data (explicit decision only) | `python manage.py migrate connect zero` |

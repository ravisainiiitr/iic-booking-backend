# Frontend Playbook

## Normal operation
- Frontend serves role-based routes and calls portal APIs successfully.

## Monitoring
- HTTP status mix, client error rates, route availability, asset integrity.

## Failure symptoms
- Blank routes, repeated auth redirects, API fetch failures.

## Diagnosis
- Validate environment config, API base URL, network/CORS, browser console errors.

## Recovery
- Roll back frontend artifact or configuration to last known-good release.

## Escalation
- Frontend on-call -> Platform on-call -> Release manager.

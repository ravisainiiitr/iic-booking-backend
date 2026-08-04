# Deployment Center Playbook

## Normal operation
- Release metadata visible and ticketed downloads valid.

## Monitoring
- Metadata endpoint response, ticket issuance rate, download failure rate.

## Failure symptoms
- Missing releases, invalid tickets, failed downloads.

## Diagnosis
- Check deployment records, storage links, auth policies, token expiry.

## Recovery
- Re-publish metadata, regenerate tickets, revert active release pointers.

## Escalation
- Release engineering -> Platform ops -> Security (if signature concerns).

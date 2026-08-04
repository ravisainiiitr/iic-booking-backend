# RAW / RESULTS Folder Configuration

Equipment-scoped directories for Remote Analysis on Windows Analysis PCs.

## Equipment fields

| Field | Purpose | Example |
|-------|---------|---------|
| `analysis_raw_data_directory` | Root for booking RAW folders | `D:\PXRD\RAW` |
| `analysis_results_directory` | Root for booking RESULTS folders | `D:\PXRD\RESULTS` |

Per booking the Agent uses:

```
{RAW}\{virtual_booking_id}
{RESULTS}\{virtual_booking_id}
```

Example: `D:\PXRD\RAW\IICPXRD[A]202600001`

## Lifecycle

1. **Prepare** — create booking RAW/RESULTS folders; mirror Portal/DSA RAW into RAW path.
2. **Session** — user works in the allocated desktop; results written under RESULTS.
3. **End Analysis** — Agent collects RESULTS → Portal workspace → S3 when configured.
4. **Cleanup** — booking folders deleted after successful upload; empty folders are not left behind.
5. **Abnormal exit** — cleanup still runs from End Analysis / session terminate / CLEAN_WORKSTATION paths.

## Security

- Users never receive absolute Windows paths in the browser experience payload (logical labels only).
- Agent confines file operations to the allocated booking folder under the equipment roots.
- Previous booking data must not remain after cleanup.

## Administrator setup

Equipment Administrator (or Approve & Create on addition requests) sets:

- Remote Analysis enabled
- Default / extension session minutes
- RAW directory
- RESULTS directory
- Required analysis software mapping

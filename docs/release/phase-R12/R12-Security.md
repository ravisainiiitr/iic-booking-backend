# R12 — Security notes (data browser)

## Authorization

| Endpoint | Rule |
| --- | --- |
| `GET .../data-browser/` | `_can_access_analysis_files` (owner or elevated analysis staff) |
| `POST .../data-selection/` | Owner launch **or** analysis-file staff; source booking also must pass file access |

Faculty / same-department summary viewers **cannot** enumerate or stage analysis data.

## Data exposure

- Metadata only in browse responses (names, sizes, types, timestamps, sources)
- No permanent public object URLs
- No presigned S3 URLs in R12 browser payloads
- Opaque `entry_key` may mirror internal merge keys but is not a download URL

## Path safety

Selection rejects relative paths containing `..` segments. Staging reuses TransferManager / RawData folder constraints.

## Isolation

R12 does not grant cross-user browse. Candidate bookings are limited to the **booking owner's** history (typically same equipment). Staff assisting a booking still only see that owner's authorized set when calling through the booking context.

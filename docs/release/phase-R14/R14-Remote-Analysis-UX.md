# R14 — Remote Analysis UX

`Open Analysis Workspace` still routes to `/analysis-workspace/:id`, but the **first screen** is data-source selection, not the technical session dashboard.

## Flow

```
Booking Details
  → Open Analysis Workspace
  → What data would you like to analyze?
  → Current / Previous / Upload
  → Confirm (Use This Data)
  → Existing RAA allocation / session lifecycle
```

## Lifecycle of the page

| State | UI |
| --- | --- |
| Before session | Three data-source cards |
| Data confirmed, PC waiting | Summary + “Your data is ready. We are waiting for a compatible Analysis PC.” |
| During session | Analysis Workspace dashboard |
| After session | Analyzed Data remains on Booking Details (not a session re-entry) |

## What was not duplicated

- RAA allocation engine
- Session / Guacamole lifecycle
- R12 data browser APIs (implemented once, reused by Current and Previous)
- Analyze Data is **not** a second entry point from Booking Details

## Session Information

After allocation, Session Information shows the configured Analysis PC input path and workspace label. These details are **not** on the first data-selection screen.

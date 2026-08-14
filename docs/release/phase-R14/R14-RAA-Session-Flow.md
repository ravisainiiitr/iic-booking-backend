# R14 — RAA Session Flow

After **Use This Data**, the existing `BookingRemoteAnalysisService.analyze_data` path runs:

```
Data selection recorded
  → Allocation engine (unchanged)
  → Workspace ensure
  → Selected data staged (filtered RAW)
  → Check-in / launch / remote desktop
```

If no compatible PC is free:

- Reservation is queued
- Selection is kept on the booking
- User sees: “Your data is ready. We are waiting for a compatible Analysis PC.”

No second allocation or session engine was added.

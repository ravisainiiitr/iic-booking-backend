# PI Security

1. **Assignment:** Only admin-panel users may assign/remove PIs via equipment APIs / Django admin.
2. **Faculty-only:** Write serializer restricts `faculty` queryset to active Faculty users.
3. **Pricing:** Profile selection is server-side (`resolve_pricing_profile_for_user`). Client flags are ignored.
4. **Historical integrity:** Booking.charge_profile FK snapshots the applied profile at confirmation.
5. **Audit:** `EquipmentPIAuditLog` records assign/remove/deactivate/reactivate and PI charge upserts.

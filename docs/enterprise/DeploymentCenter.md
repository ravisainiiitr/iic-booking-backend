# Deployment Center (Phase 2 enhancements)

Phase-1 installer catalog remains. Phase-2 adds:

- Compatibility matrix JSON on wizard releases (`compatibility`)
- Repair / emergency package file fields
- `rollback_of` pointer between releases
- SHA-256 + signature status (already Phase-1) surfaced in UI

Publish:

```bash
python manage.py publish_dsa_installer ...
python manage.py publish_ra_installer ...
python manage.py publish_equipment_wizard ...
```

UI: `/deployment-center` and Lab Infrastructure Updates tab.

# Versioning Strategy — Platform 2.5

## Policy

Use **Semantic Versioning 2.0.0** (`MAJOR.MINOR.PATCH[-prerelease]`).

| Segment | Meaning for this platform |
|---------|---------------------------|
| MAJOR | Incompatible API or control-plane break; requires coordinated agent upgrades |
| MINOR | Backward-compatible features (Phase streams: 2.0 PnP, 2.5 lifecycle/SAT) |
| PATCH | Bugfixes / security only |
| prerelease | `rc1`, `rc2`, … before GA |

---

## Component versions (proposed for this RC)

| Component | Version | Rationale |
|-----------|---------|-----------|
| Portal Backend | **2.5.0-rc1** | Phase 2.5 feature set on portal |
| Portal Frontend | **2.5.0-rc1** | Must match portal minor |
| Database (logical schema) | **2.5.0** | Tracks portal minor; not a SemVer package — label in manifest |
| DSA | **1.0.0-rc1** | First production-shaped agent line for IIC lab PnP |
| Equipment PC Wizard | **1.0.0-rc1** | Ships with DSA Phase 1 |
| RAA | **1.0.0-rc1** | First packaged agent line (repo currently unversioned) |

Align installer `version` fields in Deployment Center with these strings exactly.

---

## Git tags

| Tag | Points to |
|-----|-----------|
| `platform-v2.5.0-rc1` | Backend release commit (or mono-tag if used) |
| `frontend-v2.5.0-rc1` | Frontend release commit |
| `dsa-v1.0.0-rc1` | DSA release commit |
| `wizard-v1.0.0-rc1` | Same as DSA commit if wizard lives in DSA repo — **document which** |
| `raa-v1.0.0-rc1` | RAA release commit |

Tags are immutable; RC2 bumps prerelease (`-rc2`) or patch after GA (`2.5.1`).

---

## Compatibility JSON (Deployment Center)

Store min versions, e.g.:

```json
{
  "portal_min": "2.5.0",
  "dsa_min": "1.0.0",
  "wizard_min": "1.0.0",
  "raa_min": "1.0.0"
}
```

---

## Changelog discipline

- Every release commit message maps to [Change Log](./11-Change-Log.md).  
- Do not reuse version numbers for different SHAs.  
- Hotfix on 2.5.0 GA → `2.5.1` without waiting for Phase 3.

# Software Mapping Guide

Intelligent workstation allocation uses equipment → software catalog mappings.

## Model

- `AnalysisSoftwareCatalog` — canonical software names (e.g. CasaXPS)
- `EquipmentAnalysisSoftware` — which catalogs an equipment requires
- Agent `InstalledSoftware` inventory — what is present on each Analysis PC

## Allocation rule

`SoftwareMappingService.required_software_names(equipment)` feeds
`requested_capabilities.required_software_names` into the scheduler.

`AvailabilityEngine` **hard-filters** PCs missing any required name
(`software_name__icontains`). Soft scoring alone never allocates an incomplete PC.

## Queue UX

When matching PCs exist but all are busy:

- Title: **Waiting for an Analysis PC with the required software**
- Shows matching / busy / available counts, queue position, estimated wait

## Example (XPS / CasaXPS)

| PC | CasaXPS | Eligible |
|----|---------|----------|
| PC1–PC3 | Yes | Yes |
| PC4–PC5 | No | Never |

When PC1–PC3 are Busy, the booking queues until the first suitable PC frees.

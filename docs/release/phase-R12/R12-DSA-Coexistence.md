# R12 — DSA coexistence stubs

## Principle

**Department Sync Agent (DSA)** and **Remote Analysis Agent (RAA)** remain independent.

| Agent | Role |
| --- | --- |
| DSA | Publishes instrument results into portal storage (`Results/` S3 and/or `ResultAttachment`) |
| RAA | Syncs portal Analysis Workspace Input/Output folders on allocated PCs |

## What R12 does

- Reads DSA/operator/S3 results **through existing portal merge APIs**
- Stages selected files into the portal workspace for RAA sync
- Does **not** invent direct DSA↔RAA protocols
- Does **not** scan `C:\` or Program Files on lab PCs

## Future stubs (not implemented here)

1. Optional provenance tag `source=dsa` already exists on merge entries — UI may badge it later
2. Retention / purge policies stay on portal workspace + Results lifecycle jobs
3. If DSA layout conventions change, only `results_s3` / merge helpers need updates — browser stays metadata-facing

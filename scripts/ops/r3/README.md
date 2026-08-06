# R.3 Clean-State Preparation (All Four Machines)

Run **elevated PowerShell** on each machine before Step 1.
Do **not** run cleanup scripts on machines that must keep production data.

| Machine | Role | Script |
|---------|------|--------|
| System C | DSA (dual NIC) | `r3-clean-dsa.ps1` |
| System A | Equipment PC-1 | `r3-clean-equipment-pc.ps1` |
| System D | Equipment PC-2 | `r3-clean-equipment-pc.ps1` |
| System B | Remote Analysis | `r3-clean-raa.ps1` |

After local cleanup, in Portal (department admin / Main Admin):

1. Revoke any prior `ProvisionedDevice` rows for this lab department (or use Replace).
2. Confirm two **unassigned** DSA-enabled instruments exist for Systems A and D.
3. Confirm Remote Analysis enabled on at least one instrument for System B.
4. Set Department Provisioning Policy = **Trusted Auto-Approve** for timed qualification (or Manual if testing A5).
5. Clear pending provisioning sessions for the department.

Evidence: save `Get-Date -Format o` and script transcript under `artifacts/phase-R3/logs/`.

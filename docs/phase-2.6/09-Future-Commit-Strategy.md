# Future Commit Strategy — Phase 2.6

**Do not create commits now.** Strategy only (aligns with Release Preparation Report).

## Rules for every future commit

1. Builds on a clean tree after that commit.  
2. Targeted tests pass (or documented skip).  
3. Single purpose; reviewable in isolation.  
4. No binaries, no secrets, no `tmp_*`.  
5. Migrations accompany matching models.

## Suggested split (reminder)

### Backend
B1 Reverse tunnel → B2 RA lifecycle → B3 Equipment fields → B4 Deployment Center → B5 Sync PnP → B6 Lab fleet → B7 SAT APIs → B8 Update discover → B9 Docs → B10 Residual fixes  

### Frontend
F1 Deployment Center → F2 Lab Infrastructure → F3 SAT Dashboard → F4 RDP diagnostics → F5 optional ancillary  

### DSA
D0 Hygiene commit: `.gitignore` artifacts only *(first)* → D1 platform catch-up → D2 EqPC discovery → D3 ack/rollup → D4 Wizard → D5 Installer **source** → D6 Docs  

### RAA
R1 Initial skeleton + gitignore → R2 core agent → R3 tunnel/diagnostics → R4 update discover → R5 docs/scripts  

Full file groupings: [`docs/phase-2.5/Release-Preparation-Report-2026-08-04.md`](../phase-2.5/Release-Preparation-Report-2026-08-04.md).

## Order relative to Lab SAT

Repository Recovery commits may proceed **before** Lab SAT only when approved — product behavior unchanged.  
**Production** still requires Lab SAT GO after an RC is built from those commits.

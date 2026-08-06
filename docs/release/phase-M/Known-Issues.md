# Known Issues — v2.5.0 Final

| ID | Issue | Severity | Mitigation |
|----|-------|----------|------------|
| KI-01 | Single live DSA / single Analysis PC in current fleet | Medium | Queue expected; expand fleet for concurrency |
| KI-02 | External user result download requires verified I-STEM FBR | Info | By design; operators can still access results |
| KI-03 | `/analysis/release/` may leave reservation QUEUED | Low | Prefer `/analysis/end/` for cleanup |
| KI-04 | DSA uploads UI may show Queued after transport Completed | Low | Check UploadQueue / portal results |
| KI-05 | Orphan RA RESERVED possible without open reservation | Low | Admin `CLEAN_WORKSTATION` |
| KI-06 | Catalog cold start can be multi-second | Low | Monitor; optimize post-go-live |
| KI-07 | Destructive full host reboot drill deferred | Low | Restart policies verified; schedule quarterly |

Resolved in this train: sticky BUSY (rc23), external accept (rc24), frontend Docker unhealthy due to `localhost`→IPv6 (M.1), disk 80% (M.1 cleaned to ~39%).

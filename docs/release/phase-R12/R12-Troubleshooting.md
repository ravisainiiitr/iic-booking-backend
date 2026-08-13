# R12 Troubleshooting

## Data browser returns empty Previous

- Previous candidates require **same user**, **same equipment**, and `booking_id` less than current.
- Create previous bookings before current when testing ID ordering.
- Search uses equipment name/code, sample_name (sample_trace identifiers / notes / dynamic inputs), booking refs, folder names, and file names.

## 403 on data-browser / data-selection

- Caller must pass `_can_access_analysis_files` (owner / privileged paths).
- Faculty same-department may see booking summary elsewhere but **not** analysis files.

## Views import / service mismatch

- Use `BookingAnalysisDataBrowserService().browse/select` (not a divergent `AnalysisDataBrowserService` constructor). Fixed in `f12a8a3` / release `v2.5.38-r12-pi-pricing`.

## Tests need `--nomigrations`

Full migrate may fail on pre-existing `remote_anal_status_7b334f_idx` duplicate. Classify as existing environment/schema debt unrelated to R12.

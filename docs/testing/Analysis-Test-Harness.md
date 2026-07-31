# Analysis Platform Test Harness

Standalone regression harness for the Remote Analysis Platform. It validates the path from **completed booking → analyze → allocation → workspace/job → processed download** without changing production business logic.

## Architecture

```
tests/analysis_platform/
  seeder.py              # APT lab: software, workflows, equipment, pool, personas, booking
  data_generator.py      # Bulk bookings / workstations / RAW blobs for load tests
  mock_agent/            # HTTP mock of the Windows Remote Analysis Agent
  utils/                 # Assertions, cleanup, HTML/JSON/JUnit report builders
  reporting.py           # Dashboard aggregator from pytest JUnit
  test_api_lifecycle.py  # Booking / analyze / pause / resume / complete / files
  test_security.py       # Cross-user, injection, ops/workstation denial
  test_mock_agent.py     # Register / heartbeat / inventory / commands
  test_performance.py    # Gated by ANALYSIS_PERF=1
  test_smoke_real_agent.py  # Gated by ANALYSIS_LAB=1
  e2e/                   # Playwright browser suite (ANALYSIS_E2E=1)
  report/                # Generated HTML / JSON / JUnit artifacts
scripts/run-analysis-tests.ps1
scripts/run-analysis-tests.sh
```

**Reuse:** fixtures follow the same patterns as `iic_booking/remote_analysis/tests/` (APIClient, UserFactory, mock Guacamole via `RemoteAnalysisSettings.mock_guacamole=True`). Markers mirror SAT gates (`sat` / `sat_lab` / `sat_perf`).

Production code is not modified by this harness.

## How to run

From the backend repo root:

```powershell
# Full API + security + mock-agent suite + HTML dashboard
.\scripts\run-analysis-tests.ps1

# Include performance
.\scripts\run-analysis-tests.ps1 -Perf

# Real Windows agent smoke (requires ANALYSIS_AGENT_ID)
.\scripts\run-analysis-tests.ps1 -Lab -AgentId "your-agent-id"

# Playwright (frontend must be running; set E2E env vars — see below)
.\scripts\run-analysis-tests.ps1 -E2E
```

```bash
./scripts/run-analysis-tests.sh
./scripts/run-analysis-tests.sh --perf
./scripts/run-analysis-tests.sh --lab --agent-id your-agent-id
./scripts/run-analysis-tests.sh --e2e
```

Pytest-only:

```bash
python -m pytest -m analysis_platform tests/analysis_platform -q
```

Artifacts land in `tests/analysis_platform/report/`:

| File | Purpose |
|------|---------|
| `dashboard.html` | Human summary |
| `dashboard.json` | Machine summary |
| `dashboard-junit.xml` / `pytest-junit.xml` | CI JUnit |
| `playwright-html/` | Browser report (when E2E runs) |

## Test seeder

`AnalysisPlatformSeeder` creates:

- Software: Notepad, Origin Test, MATLAB Test
- Workflows: single-step + multi-step (mapped to equipment)
- Equipment: PXRD Test Equipment (`APT…` code)
- Pool: one mock workstation with inventory + agent token
- Personas: researcher, lab incharge, administrator, other researcher
- One completed booking with `analysis_available=True`

Researcher password for E2E: `apt-test-password`.

## Mock Analysis Agent

`MockAnalysisAgent` speaks the real agent control plane:

1. Register (optional) / attach seeded token
2. Heartbeat (`CPU` / `Memory` / `Disk`)
3. Inventory advertise
4. Poll commands → complete PREPARE / COLLECT / CLEAN / PING

No Windows RDP or Guacamole client is required. Guacamole is left in **mock** mode by the seeder (`mock_guacamole=True`).

Standalone loop:

```bash
python -m tests.analysis_platform.mock_agent.runner
```

## Real Agent smoke (`ANALYSIS_LAB=1`)

Verifies against a live Windows agent:

- Online + fresh heartbeat
- Software inventory present
- Analyze allocation / launch URL (stops **before** desktop app interaction)
- Anonymous heartbeat rejected

Set `ANALYSIS_AGENT_ID` to the registered agent id. Human OriginPro/MATLAB work remains manual.

## Playwright

Located under `tests/analysis_platform/e2e/`.

```bash
export ANALYSIS_E2E=1
export ANALYSIS_E2E_BASE_URL=http://localhost:5173
export ANALYSIS_E2E_EMAIL=apt-researcher-XXXX@example.com
export ANALYSIS_E2E_PASSWORD=apt-test-password
export ANALYSIS_E2E_BOOKING_ID=<booking_id>
cd tests/analysis_platform/e2e && npm install && npx playwright install chromium && npx playwright test
```

Screenshots / traces are retained on failure via Playwright config.

## CI/CD integration

Suggested pipeline stage:

1. `pip install` backend test deps
2. `python -m pytest -m analysis_platform tests/analysis_platform --junitxml=…`
3. Publish `dashboard.html` + JUnit
4. Optional nightly: `ANALYSIS_PERF=1`
5. Optional lab runner: `ANALYSIS_LAB=1` + agent id secret
6. Optional E2E job with frontend preview URL

Do **not** run `ANALYSIS_CLEANUP=1` against production databases.

## Known limitations

- Mock agent simulates file staging/upload; it does not write real SMB/S3 blobs.
- Pause/resume/complete step assertions accept `400` when the job has not reached a pausable/completable state yet (timing soft-gate).
- Playwright selectors are intentionally flexible; UI copy changes may need selector updates.
- Concurrent allocation perf uses threads + Django test client; treat results as relative baselines, not absolute SLAs.
- Idle reservation cleanup (R2) is out of scope for this harness.

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `can_analyze` false | Seeder settings: `analyze_data_require_s3_files=False`; booking `COMPLETED` + `analysis_available` |
| Register 403 | `RA_AGENT_ENROLLMENT_KEY` set in env — pass enrollment key or unset for tests |
| Heartbeat 401 | Use `from_seed` token + `X-Agent-Id` matching `agent_id` |
| Workstation list 403 for researcher | Expected — enumeration is staff-only |
| Perf tests skipped | Export `ANALYSIS_PERF=1` |
| Lab tests skipped | Export `ANALYSIS_LAB=1` and `ANALYSIS_AGENT_ID` |
| Playwright skipped | Export `ANALYSIS_E2E=1` and credential/booking env vars; start Vite frontend |

## Markers (`pyproject.toml`)

- `analysis_platform` — default harness
- `analysis_perf` — load / latency
- `analysis_lab` — real agent smoke

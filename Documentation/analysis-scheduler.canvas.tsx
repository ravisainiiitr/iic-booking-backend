import {
  Callout,
  Card,
  CardBody,
  CardHeader,
  Code,
  Divider,
  Grid,
  H1,
  H2,
  H3,
  Pill,
  Row,
  Stack,
  Stat,
  Table,
  Text,
} from "cursor/canvas";

const MODELS = [
  "AnalysisReservation",
  "ReservationHistory",
  "ReservationQueue",
  "AllocationRule",
  "MaintenanceWindow",
  "SoftwareRequirement",
  "ReservationEvent",
  "ReservationAudit",
  "ReservationConflict",
  "ReservationPreference",
  "SchedulerTelemetry",
];

const STATES = [
  "REQUESTED",
  "VALIDATING",
  "QUEUED",
  "RESERVED",
  "PREPARING",
  "READY",
  "ACTIVE",
  "COMPLETED",
  "EXPIRED",
  "CANCELLED",
  "FAILED",
];

const SCORE_FACTORS = [
  "Health Score",
  "CPU Load",
  "Memory Usage",
  "Recent Usage",
  "Software Match",
  "Capability Match",
  "Department Affinity",
  "Idle Time",
];

const APIS = [
  ["POST/GET", "/api/v1/analysis/reservations/"],
  ["GET", "/api/v1/analysis/reservations/{id}/"],
  ["POST", ".../cancel/ | .../extend/"],
  ["GET", "/api/v1/analysis/availability/"],
  ["GET", "/api/v1/analysis/candidates/"],
  ["GET", "/api/v1/analysis/scheduler/status/"],
  ["GET", "/api/v1/analysis/queue/"],
];

const JOBS = [
  "expire_reservations",
  "process_reservation_queue",
  "refresh_workstation_health",
  "monitor_maintenance_windows",
  "detect_reservation_conflicts",
  "refresh_availability_snapshot",
];

export default function AnalysisSchedulerCanvas() {
  return (
    <Stack gap={24} style={{ padding: 24, maxWidth: 1100 }}>
      <Stack gap={8}>
        <H1>Remote Analysis Scheduler — Milestone 3</H1>
        <Text tone="secondary">
          Intelligent workstation reservation and allocation. Portal decides which
          machine and when — never launches Guacamole or browser sessions.
        </Text>
        <Row gap={8} style={{ flexWrap: "wrap" }}>
          <Pill tone="success">Reservation engine</Pill>
          <Pill tone="info">Candidate scoring</Pill>
          <Pill>Priority queue</Pill>
          <Pill>No Guacamole</Pill>
          <Pill>No RDP launch</Pill>
        </Row>
      </Stack>

      <Callout tone="info" title="Boundary">
        Connection happens in a later milestone. This release covers eligibility,
        scoring, reservation lifecycle, conflicts, maintenance windows, telemetry,
        and dashboards.
      </Callout>

      <Grid columns={4} gap={12}>
        <Stat label="Models" value={String(MODELS.length)} />
        <Stat label="Lifecycle states" value={String(STATES.length)} />
        <Stat label="Score factors" value={String(SCORE_FACTORS.length)} />
        <Stat label="Celery jobs" value={String(JOBS.length)} />
      </Grid>

      <Card>
        <CardHeader>Architecture</CardHeader>
        <CardBody>
          <Stack gap={6}>
            <Text weight="semibold">Portal</Text>
            <Text tone="secondary">
              ReservationEngine · Scheduler · Availability · Eligibility · Conflict
              Resolver · Queue
            </Text>
            <Divider />
            <Text weight="semibold">Agents</Text>
            <Text tone="secondary">
              Report status only. Allocation never opens a remote desktop session.
            </Text>
          </Stack>
        </CardBody>
      </Card>

      <Stack gap={8}>
        <H2>Reservation lifecycle</H2>
        <Row gap={6} style={{ flexWrap: "wrap" }}>
          {STATES.map((s) => (
            <span key={s}>
              <Pill
                tone={
                  s === "RESERVED" || s === "READY"
                    ? "success"
                    : s === "FAILED" || s === "EXPIRED"
                      ? "warning"
                      : "neutral"
                }
              >
                {s}
              </Pill>
            </span>
          ))}
        </Row>
        <Text size="small" tone="secondary">
          History persisted in ReservationHistory. Queue states: Waiting → Allocating →
          Reserved | Expired | Cancelled.
        </Text>
      </Stack>

      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>Allocation algorithm</CardHeader>
          <CardBody>
            <Stack gap={6}>
              <Text>1. Validate reservation + booking link</Text>
              <Text>2. Filter eligible workstations</Text>
              <Text>3. Score candidates with configurable weights</Text>
              <Text>4. Reserve highest score</Text>
              <Text>5. Else enqueue (priority, FIFO within)</Text>
            </Stack>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Never allocate</CardHeader>
          <CardBody>
            <Stack gap={6}>
              <Text>Offline / disabled / maintenance</Text>
              <Text>Low health / expired agent token</Text>
              <Text>Maintenance window overlap</Text>
              <Text>Missing software / capabilities / licenses</Text>
              <Text>Existing reservation conflict</Text>
            </Stack>
          </CardBody>
        </Card>
      </Grid>

      <Stack gap={8}>
        <H2>Candidate scoring factors</H2>
        <Row gap={6} style={{ flexWrap: "wrap" }}>
          {SCORE_FACTORS.map((f) => (
            <span key={f}>
              <Pill tone="info">{f}</Pill>
            </span>
          ))}
        </Row>
      </Stack>

      <Stack gap={8}>
        <H2>Models</H2>
        <Row gap={6} style={{ flexWrap: "wrap" }}>
          {MODELS.map((m) => (
            <span key={m}>
              <Pill>{m}</Pill>
            </span>
          ))}
        </Row>
      </Stack>

      <Grid columns={2} gap={16}>
        <Stack gap={8}>
          <H3>APIs</H3>
          <Table headers={["Method", "Path"]} rows={APIS.map(([m, p]) => [m, p])} />
        </Stack>
        <Stack gap={8}>
          <H3>Celery jobs</H3>
          <Row gap={6} style={{ flexWrap: "wrap" }}>
            {JOBS.map((j) => (
              <span key={j}>
                <Pill tone="success">{j}</Pill>
              </span>
            ))}
          </Row>
          <Text size="small" tone="secondary">
            Linked to equipment bookings via <Code>booking</Code> FK — no duplicate
            booking engine.
          </Text>
        </Stack>
      </Grid>

      <Card>
        <CardHeader>Future session launch</CardHeader>
        <CardBody>
          <Text tone="secondary">
            Authenticate → allocate (done here) → create Guacamole connection → notify
            agent prepare → Portal launches browser session → end → cleanup.
          </Text>
        </CardBody>
      </Card>

      <Text size="small" tone="secondary">
        Source: Milestone 3 · iic_booking.remote_analysis
      </Text>
    </Stack>
  );
}

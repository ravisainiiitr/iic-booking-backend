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
  "AnalysisWorkstation",
  "WorkstationHeartbeat",
  "WorkstationInventory",
  "InstalledSoftware",
  "SoftwareLicense",
  "RemoteCommand",
  "CommandExecution",
  "AgentToken",
  "WorkstationEvent",
  "TelemetrySnapshot",
  "WorkstationCapability",
  "WorkstationStateHistory",
];

const AGENT_APIS = [
  ["POST", "/api/v1/analysis/register/"],
  ["POST", "/api/v1/analysis/heartbeat/"],
  ["POST", "/api/v1/analysis/inventory/"],
  ["GET", "/api/v1/analysis/commands/"],
  ["POST", "/api/v1/analysis/commands/{id}/complete/"],
];

const ADMIN_APIS = [
  ["GET", "/api/v1/analysis/workstations/"],
  ["GET", "/api/v1/analysis/workstations/{id}/"],
  ["GET", "/api/v1/analysis/dashboard/"],
  ["POST", ".../maintenance|enable|disable/"],
  ["POST", ".../workstations/{id}/commands/"],
];

const SERVICES = [
  "RegistrationService",
  "HeartbeatService",
  "InventoryService",
  "CommandService",
  "HealthEngine",
  "WorkstationAdminService",
  "Audit (WorkstationEvent)",
];

const UI_TABS = [
  "Dashboard",
  "Workstations",
  "Installed Software",
  "Heartbeat History",
  "Commands",
  "Maintenance",
  "Health",
  "Audit",
];

export default function RemoteAnalysisPortalCanvas() {
  return (
    <Stack gap={24} style={{ padding: 24, maxWidth: 1100 }}>
      <Stack gap={8}>
        <H1>Remote Analysis Portal — Milestone 2</H1>
        <Text tone="secondary">
          Enterprise workstation registry inside the Equipment Booking Portal.
          Portal is the single source of truth; agents keep only a local cache.
        </Text>
        <Row gap={8} style={{ flexWrap: "wrap" }}>
          <Pill tone="success">Django remote_analysis</Pill>
          <Pill tone="info">Agent token auth</Pill>
          <Pill>No Guacamole</Pill>
          <Pill>No scheduler</Pill>
          <Pill>No browser sessions</Pill>
        </Row>
      </Stack>

      <Callout tone="info" title="Orchestration boundary">
        Guacamole, session allocation, and browser RDP remain future Portal
        milestones. This release covers registration, authentication, heartbeats,
        inventory, health, command queue, admin UI, RBAC, and audit.
      </Callout>

      <Grid columns={4} gap={12}>
        <Stat label="Models" value={String(MODELS.length)} />
        <Stat label="Agent APIs" value={String(AGENT_APIS.length)} />
        <Stat label="UI tabs" value={String(UI_TABS.length)} />
        <Stat label="Health score" value="0–100" />
      </Grid>

      <Card>
        <CardHeader>Architecture</CardHeader>
        <CardBody>
          <Stack gap={8}>
            <Text weight="semibold">Equipment Booking Portal → remote_analysis</Text>
            <Text tone="secondary">
              Workstations · Inventory · Commands · Monitoring · Health · Audit ·
              Telemetry
            </Text>
            <Divider />
            <Text weight="semibold">Remote Analysis Agents</Text>
            <Text tone="secondary">
              PC-01 … PC-N report status and execute trusted Portal commands only.
            </Text>
          </Stack>
        </CardBody>
      </Card>

      <Grid columns={2} gap={16}>
        <Card>
          <CardHeader>Registration</CardHeader>
          <CardBody>
            <Stack gap={6}>
              <Text>1. Agent POSTs register with AgentId + inventory</Text>
              <Text>2. Portal prevents duplicates</Text>
              <Text>3. Issues hashed AgentToken (plaintext once)</Text>
              <Text>4. Agent stores token; later calls use Bearer + X-Agent-Id</Text>
            </Stack>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Heartbeat & health</CardHeader>
          <CardBody>
            <Stack gap={6}>
              <Text>Stores CPU / RAM / disk / GPU / uptime / idle / user</Text>
              <Text>Alerts: high CPU, low memory, disk full</Text>
              <Text>Missed heartbeats → OFFLINE</Text>
              <Text>Health score from freshness, resources, inventory, failures</Text>
            </Stack>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Inventory</CardHeader>
          <CardBody>
            <Stack gap={6}>
              <Text>Hardware + software + licenses + capabilities</Text>
              <Text>Detect ADDED / REMOVED / VERSION_CHANGED</Text>
              <Text>Update only changed records; audit each sync</Text>
            </Stack>
          </CardBody>
        </Card>
        <Card>
          <CardHeader>Command queue</CardHeader>
          <CardBody>
            <Stack gap={6}>
              <Text>PENDING → DELIVERED → COMPLETED / FAILED / EXPIRED</Text>
              <Text>
                PING, REFRESH, REFRESH_SOFTWARE, COLLECT_LOGS, RESTART_AGENT,
                PREPARE_WORKSTATION, CLEAN_WORKSTATION
              </Text>
              <Text>Full CommandExecution history</Text>
            </Stack>
          </CardBody>
        </Card>
      </Grid>

      <Stack gap={8}>
        <H2>Services</H2>
        <Row gap={6} style={{ flexWrap: "wrap" }}>
          {SERVICES.map((s) => (
            <span key={s}>
              <Pill>{s}</Pill>
            </span>
          ))}
        </Row>
      </Stack>

      <Stack gap={8}>
        <H2>Models</H2>
        <Row gap={6} style={{ flexWrap: "wrap" }}>
          {MODELS.map((m) => (
            <span key={m}>
              <Pill tone="info">{m}</Pill>
            </span>
          ))}
        </Row>
      </Stack>

      <Grid columns={2} gap={16}>
        <Stack gap={8}>
          <H3>Agent APIs</H3>
          <Table
            headers={["Method", "Path"]}
            rows={AGENT_APIS.map(([m, p]) => [m, p])}
          />
        </Stack>
        <Stack gap={8}>
          <H3>Admin APIs</H3>
          <Table
            headers={["Method", "Path"]}
            rows={ADMIN_APIS.map(([m, p]) => [m, p])}
          />
        </Stack>
      </Grid>

      <Stack gap={8}>
        <H2>UI — /remote-analysis</H2>
        <Row gap={6} style={{ flexWrap: "wrap" }}>
          {UI_TABS.map((t) => (
            <span key={t}>
              <Pill tone="success">{t}</Pill>
            </span>
          ))}
        </Row>
        <Text tone="secondary" size="small">
          Manage roles: admin, dept_admin, manager (+ remote_analysis.manage). View:
          operator (+ remote_analysis.view). Students denied by default.
        </Text>
      </Stack>

      <Card>
        <CardHeader>Security</CardHeader>
        <CardBody>
          <Text tone="secondary">
            Tokens hashed with rotation/revocation. No Guacamole config, RDP
            passwords, or end-user credentials. Future flow: authenticate → allocate →
            create Guacamole connection → notify agent → prepare → Portal launches
            browser session → cleanup.
          </Text>
          <Text size="small" tone="secondary" style={{ marginTop: 8 }}>
            Permission codes: <Code>remote_analysis.manage</Code>,{" "}
            <Code>remote_analysis.view</Code>
          </Text>
        </CardBody>
      </Card>

      <Text tone="secondary" size="small">
        Source: Milestone 2 acceptance criteria · iic_booking.remote_analysis
      </Text>
    </Stack>
  );
}

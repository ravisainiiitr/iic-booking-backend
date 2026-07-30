# 03 — Expected API Sequence

Base prefix: `/api/v1/analysis/`  
Auth agent: `Authorization: Bearer <agent_token>` + `X-Agent-Id: <agentId>`  
Auth portal manage: `Authorization: Token <key>` or Django session (+ CSRF on unsafe methods)

---

## A. Agent registration & keep-alive

```http
POST /api/v1/analysis/register/
Content-Type: application/json

{"agentId":"…","hostname":"…","displayName":"…","cpuCores":8,"memoryGB":32,"agentVersion":"…"}
# Optional: X-Enrollment-Key when RA_AGENT_ENROLLMENT_KEY is set

→ 200/201 {"accepted":true,"agent_id":"…","token":"<once>"}

POST /api/v1/analysis/heartbeat/
Authorization: Bearer …
X-Agent-Id: …

{"cpuPercent":…,"memoryPercent":…,"diskPercent":…,…}

POST /api/v1/analysis/inventory/
Authorization: Bearer …
```

## B. Command poll / complete

```http
GET /api/v1/analysis/commands/
Authorization: Bearer …

POST /api/v1/analysis/commands/{command_id}/complete/
{"success":true,"message":"…","result":{…}}
```

## C. Commissioning / sync workflow (portal manage)

```http
GET /api/v1/analysis/operations/commissioning/?view=html
# anonymous → 302 FRONTEND_URL/login?next=…
# or ?view=html&token=<drf> → 302 token-stripped + session

GET /api/v1/analysis/operations/commissioning/?workspace_id={uuid}
Accept: application/json

POST /api/v1/analysis/operations/commissioning/action/
{"action":"create","booking_id":12345,"workstation_id":"{uuid}","ingest":false}

POST /api/v1/analysis/operations/commissioning/action/
Content-Type: multipart/form-data
action=upload&workspace_id={uuid}&folder=RawData&file=@sample-input.txt

POST /api/v1/analysis/operations/commissioning/action/
{"action":"prepare","workspace_id":"{uuid}"}
```

### Agent side after PREPARE

```http
GET /api/v1/analysis/commands/
GET /api/v1/analysis/workspaces/{id}/manifest/
GET /api/v1/analysis/workspaces/{id}/files/{file_id}/content/
POST /api/v1/analysis/commands/{id}/complete/
{"success":true,"message":"Prepared …; Downloaded N file(s)…"}
```

### Collect + cleanup

```http
POST /api/v1/analysis/operations/commissioning/action/
{"action":"collect","workspace_id":"{uuid}"}

# Agent:
GET /api/v1/analysis/commands/
POST /api/v1/analysis/workspaces/{id}/agent-upload/   # Output → Processed
POST /api/v1/analysis/commands/{id}/complete/

POST /api/v1/analysis/operations/commissioning/action/
{"action":"cleanup","workspace_id":"{uuid}"}
```

## D. Result access (portal)

```http
GET /api/v1/analysis/workspaces/{id}/files/
GET /api/v1/analysis/workspaces/{id}/download/
# or file content endpoints per workspace API
```

## E. Security negative sequence

```http
GET /api/v1/analysis/operations/commissioning/
→ 401/403 {"detail":"Authentication credentials were not provided."}

GET /api/v1/analysis/operations/commissioning/?token=<valid>
→ 401/403   # query token must NOT auth JSON

GET /api/v1/analysis/commands/
# missing/invalid Bearer → 401/403
```

## F. Sequence diagram (happy path)

```
Operator          Portal              Agent
   |                |                   |
   |-- create ----->|                   |
   |-- upload ----->|                   |
   |-- prepare ---->|-- PREPARE cmd --->|
   |                |<-- manifest/get --|
   |                |<-- complete ------|
   |                |   InputReady      |
   |  (manual Output on disk)           |
   |-- collect ---->|-- COLLECT ------->|
   |                |<-- agent-upload --|
   |                |<-- complete ------|
   |                |  UploadVerified   |
   |-- cleanup ---->|-- CLEAN --------->|
   |                |   AVAILABLE       |
```

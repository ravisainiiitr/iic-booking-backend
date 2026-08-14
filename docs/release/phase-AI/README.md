# Phase AI — IIC Research Copilot & Mobile Production Qualification

| Phase | Doc | Status |
|-------|-----|--------|
| AI.1 | [AI.1-Research-Copilot-Conversation-Framework.md](./AI.1-Research-Copilot-Conversation-Framework.md) | Implemented |
| AI.2 | [AI.2-Knowledge-Engine.md](./AI.2-Knowledge-Engine.md) | Implemented |
| AI.3 | [AI.3-Copilot-Tools-and-Mobile.md](./AI.3-Copilot-Tools-and-Mobile.md) | Implemented |
| AI.4 | [AI.4-Completion-Report.md](./AI.4-Completion-Report.md) | Implemented |
| AI.5 | [AI.5-Staging-E2E-Report.md](./AI.5-Staging-E2E-Report.md) | Implemented (staging later BLOCKED) |
| AI.6 | [AI.6-E2E-Qualification-Report.md](./AI.6-E2E-Qualification-Report.md) | Implemented |
| AI.7 | [AI.7-Android-E2E-Qualification-Report.md](./AI.7-Android-E2E-Qualification-Report.md) | Implemented |
| AI.8 | [AI.8-Production-Readiness-Report.md](./AI.8-Production-Readiness-Report.md) | Implemented |
| AI.9 | [AI.9-Staged-Pilot-Qualification-Report.md](./AI.9-Staged-Pilot-Qualification-Report.md) | Implemented |
| AI.10 | [AI.10-Limited-Production-Pilot-Readiness.md](./AI.10-Limited-Production-Pilot-Readiness.md) | Implemented — LIMITED PILOT READY |
| AI.10 | [AI.10-Limited-Production-Pilot-Runbook.md](./AI.10-Limited-Production-Pilot-Runbook.md) | Ops runbook |
| AI.10 | [AI.10-Pilot-Support-Matrix.md](./AI.10-Pilot-Support-Matrix.md) | Support matrix |
| AI.10 | [AI.10-Pilot-Checklist.md](./AI.10-Pilot-Checklist.md) | Pilot checklist |
| AI.11 | [AI.11-Pilot-Closure-and-Promotion-Report.md](./AI.11-Pilot-Closure-and-Promotion-Report.md) | Pilot closure / promotion gates |
| AI.12 | [AI.12-Live-Pilot-Qualification-Report.md](./AI.12-Live-Pilot-Qualification-Report.md) | Live qualification — LIMITED EXTENDED |
| AI.13 | [AI.13-Copilot-Assessment.md](./AI.13-Copilot-Assessment.md) | Copilot inventory |
| AI.13 | [AI.13-Copilot-Production-Deployment-Report.md](./AI.13-Copilot-Production-Deployment-Report.md) | Copilot → LIMITED PILOT READY (flag OFF) |
| AI.14 | [AI.14-Implementation-Assessment.md](./AI.14-Implementation-Assessment.md) | Full functional inventory |
| AI.14 | [AI.14-Full-Copilot-Implementation-Report.md](./AI.14-Full-Copilot-Implementation-Report.md) | Full implementation — LIMITED PILOT READY (flag OFF) |
| AI.15 | [AI.15-Copilot-Live-Pilot-Qualification.md](./AI.15-Copilot-Live-Pilot-Qualification.md) | Live qualification — **NOT READY** (pre-deploy) |
| AI.16 | [AI.16-Copilot-Production-Enablement-Report.md](./AI.16-Copilot-Production-Enablement-Report.md) | **DEPLOYED — PILOT BLOCKED** (OpenAI + pilot account) |
| AI.17 | [AI.17-Implementation-Assessment.md](./AI.17-Implementation-Assessment.md) | Source inventory before completion |
| AI.17 | [AI.17-Ollama-Architecture.md](./AI.17-Ollama-Architecture.md) | Provider + isolation architecture |
| AI.17 | [AI.17-Production-Deployment.md](./AI.17-Production-Deployment.md) | Deploy order / pilot / rollback |
| AI.17 | [AI.17-Security.md](./AI.17-Security.md) | AuthZ, injection, audit |
| AI.17 | [AI.17-Performance.md](./AI.17-Performance.md) | Limits + isolation |
| AI.17 | [AI.17-Test-Report.md](./AI.17-Test-Report.md) | Test evidence |
| AI.17 | [AI.17-Copilot-Implementation-Report.md](./AI.17-Copilot-Implementation-Report.md) | **PARTIAL — BLOCKED** (code ready; prod pilot blocked) |
| AI.17 | [AI.17-Ollama-Assessment.md](./AI.17-Ollama-Assessment.md) | Earlier Ollama inventory |
| AI.17 | [AI.17-Ollama-Setup.md](./AI.17-Ollama-Setup.md) | Local Ollama setup |
| AI.17 | [AI.17-Ollama-Implementation-and-Pilot-Report.md](./AI.17-Ollama-Implementation-and-Pilot-Report.md) | Prior Ollama provider report |
| AI.18 | [AI.18-Production-Integration-Assessment.md](./AI.18-Production-Integration-Assessment.md) | Production audit |
| AI.18 | [AI.18-Integration-Report.md](./AI.18-Integration-Report.md) | Surgical integrate onto master |
| AI.18.1 | [AI.18.1-EC2-Resource-Qualification.md](./AI.18.1-EC2-Resource-Qualification.md) | Old EC2 resource blocker |
| AI.19 | [AI.19-EC2-Ollama-Baseline.md](./AI.19-EC2-Ollama-Baseline.md) | Pre-Ollama baseline on m5a.2xlarge |
| AI.19 | [AI.19-Ollama-Production-Deployment.md](./AI.19-Ollama-Production-Deployment.md) | **PARTIAL** — Ollama live private; Copilot OFF |
| AI.20 | [AI.20-Functional-Qualification-Report.md](./AI.20-Functional-Qualification-Report.md) | Functional matrix + routing fixes |
| AI.20 | [AI.20-Final-Qualification-Report.md](./AI.20-Final-Qualification-Report.md) | **PARTIAL — PILOT BLOCKED** |

## Current production posture (AI.20)

| Layer | Status |
|-------|--------|
| CORE PLATFORM | Healthy on `3.110.50.174` (DNS still pending) |
| RESEARCH COPILOT feature flag | **`RESEARCH_COPILOT_ENABLED=false`** |
| LLM provider | **Ollama** `llama3.2:1b` via AI.17 gateway |
| Live Ollama on prod | **Private** (2 CPU / 8 GB, concurrent 1, no public 11434) |
| Functional qualification | **Advanced** (grounding/tools/authz/confirm/injection/busy/timeout) |
| Pilot allowlist | **Empty** |
| Controlled pilot | **BLOCKED** (need real authorized emails) |
| DSA/RAA live under Copilot | **BLOCKED BY DNS** |
| Broader / global Copilot | **NOT READY** — keep OFF |

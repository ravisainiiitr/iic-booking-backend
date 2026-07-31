# Deployment Checklist — Phase 4 Commissioning

- [ ] Deploy Portal build with Live Commissioning + fault injection endpoints
- [ ] Deploy / verify Reverse Tunnel Gateway health + metrics
- [ ] Deploy / verify Guacamole + guacd
- [ ] Install/upgrade Windows Remote Analysis Agent on target PC
- [ ] Confirm Department Sync Agent (DSA) running for RawData
- [ ] Env: tunnel admin URL, WSS URL, adapter hostname, token secret (secrets not in ZIP)
- [ ] Compose/stack healthy (`docker compose ps` / ECS equivalent)
- [ ] Run harness: `pytest tests/analysis_platform/test_commissioning.py iic_booking/remote_analysis/tests/test_commissioning_toolkit.py -q`
- [ ] Open Live Commissioning HTML as manage-permission admin

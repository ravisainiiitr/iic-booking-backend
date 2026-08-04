# RAA Playbook

## Normal operation
- RAA registered, heartbeating, and handling session/tunnel commands.

## Monitoring
- Heartbeat freshness, tunnel state, command completion lag, workspace transfer state.

## Failure symptoms
- Session launch failures, tunnel disconnect loops, upload completion failures.

## Diagnosis
- Check RAA logs, portal command queue, guacamole/gateway path, credentials.

## Recovery
- Restart RAA service, re-establish tunnel, retry safe session steps.

## Escalation
- Lab ops -> RAA support -> Remote analysis platform owner.

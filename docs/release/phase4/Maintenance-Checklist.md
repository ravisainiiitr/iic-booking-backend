# Maintenance Checklist — Remote Analysis

## Daily

- [ ] Live Commissioning overall not RED
- [ ] Agent heartbeats current
- [ ] No stuck ACTIVE tunnels (> max lifetime)
- [ ] No orphan BUSY workstations

## Weekly

- [ ] Review TunnelEvent / reconnect spikes
- [ ] Rotate evidence archive storage
- [ ] Guacamole idle timeout behaviour sample
- [ ] Disk on Analysis PC + Portal workspace root

## After change

- [ ] Start CommissioningRun → connectivity + one live path smoke
- [ ] Download evidence ZIP
- [ ] Update Known-Issues.md if needed

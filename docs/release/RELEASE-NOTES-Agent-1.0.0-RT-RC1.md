# Release Notes — RemoteAnalysisAgent `1.0.0-RT-RC1`

(Canonical copy: Agent repo `docs/RELEASE-NOTES-1.0.0-RT-RC1.md`.)

## Summary

Agent RC with `JOIN_TUNNEL` / `CLOSE_TUNNEL` and tunnel WSS client for Platform `1.0.0-RT-RC1`.

## Highlights

Tunnel package · framing tests · version `1.0.0-RT-RC1` · bundled hardening required to compile

## Security

Portal-issued tokens only; path safety helpers; exclude local secret appsettings from git.

## Operational changes

Windows service upgrade on Analysis PC; handlers idle until JOIN commands.

## Deployment notes

Upgrade after Portal/Gateway when preparing reverse_tunnel commissioning.

## Breaking changes

None for existing HTTPS agent APIs.

## Known limitations

.NET 10 Windows runtime; RT-only split requires future refactor of Program.cs dependencies.

## Rollback

Reinstall previous Agent build.

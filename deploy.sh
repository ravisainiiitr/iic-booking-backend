#!/usr/bin/env bash
# Convenience wrapper — see scripts/deploy/deploy.sh
exec "$(cd "$(dirname "$0")" && pwd)/scripts/deploy/deploy.sh" "$@"

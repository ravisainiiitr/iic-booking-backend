"""Fail-closed guards for the local acceptance settings (``config.settings.acceptance_local``).

Pure functions so they can be unit-tested without loading those settings:

* ``check_environment``: the process must opt in explicitly (``IIC_ACCEPTANCE_ENV=local-acceptance``) and must not
  look like a deployment (container, CI, production settings module).
* ``check_database_url``: PostgreSQL on this machine, database name exactly ``iic_extbook_accept_dev``.
* ``check_connected_database``: run on every new database connection; the server must report a loopback address and
  the database must carry the disposable marker (``COMMENT ON DATABASE ... IS '<DISPOSABLE_MARKER>'``), which only
  ``scripts/extbook_local/extbook-local.ps1 create-db`` sets.
* ``install_network_guard``: DNS lookups and socket connections to anything but loopback raise immediately.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

ACCEPTANCE_DATABASE_NAME = "iic_extbook_accept_dev"
ACCEPTANCE_ENV_VAR = "IIC_ACCEPTANCE_ENV"
ACCEPTANCE_ENV_VALUE = "local-acceptance"
DISPOSABLE_MARKER = "iic-extbook-local-acceptance:disposable"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class AcceptanceGuardError(RuntimeError):
    """Raised when the acceptance environment is not demonstrably local and disposable."""


class OutboundNetworkBlocked(ConnectionRefusedError):
    """A non-loopback connection or DNS lookup was attempted under the acceptance settings."""


def check_environment(environ=None, *, path_exists=os.path.exists) -> None:
    env = os.environ if environ is None else environ
    if env.get(ACCEPTANCE_ENV_VAR) != ACCEPTANCE_ENV_VALUE:
        raise AcceptanceGuardError(
            f"acceptance settings need {ACCEPTANCE_ENV_VAR}={ACCEPTANCE_ENV_VALUE} (set by extbook-local.ps1).",
        )
    if path_exists("/.dockerenv") or env.get("KUBERNETES_SERVICE_HOST") or env.get("CI") or env.get("GITHUB_ACTIONS"):
        raise AcceptanceGuardError("acceptance settings refuse to run in a container or CI job.")


def check_database_url(url: str) -> tuple[str, int, str]:
    """Return (host, port, name) of a valid local acceptance DATABASE_URL, else raise."""
    if not url:
        raise AcceptanceGuardError("DATABASE_URL is not set.")
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql", "pgsql"):
        raise AcceptanceGuardError("DATABASE_URL must be a PostgreSQL URL.")
    host = (parts.hostname or "").lower()
    if host not in LOCAL_HOSTS:
        raise AcceptanceGuardError(f"refusing database host {host!r}; only this machine is allowed.")
    name = parts.path.lstrip("/")
    if name != ACCEPTANCE_DATABASE_NAME:
        raise AcceptanceGuardError(f"refusing database {name!r}; only {ACCEPTANCE_DATABASE_NAME!r} is allowed.")
    if parts.query:
        raise AcceptanceGuardError("DATABASE_URL options are not allowed (they could redirect the connection).")
    return host, parts.port or 5432, name


def check_connected_database(server_address: str | None, database: str, comment: str | None) -> None:
    if database != ACCEPTANCE_DATABASE_NAME:
        raise AcceptanceGuardError(f"connected to {database!r}, not {ACCEPTANCE_DATABASE_NAME!r}.")
    if server_address is not None and not _is_loopback(server_address):
        raise AcceptanceGuardError(f"database server address {server_address!r} is not loopback.")
    if (comment or "").strip() != DISPOSABLE_MARKER:
        raise AcceptanceGuardError(
            f"database {database!r} lacks the disposable marker; create it with extbook-local.ps1 create-db.",
        )


def _is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    host = str(host).strip("[]").split("%")[0].lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def install_network_guard():
    """Block non-loopback DNS and socket connections in this process. Returns a function that removes the guard."""
    original = (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, socket.create_connection,
                socket.socket.sendto)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback(host):
            raise OutboundNetworkBlocked(f"acceptance: DNS lookup of {host!r} blocked (only loopback is allowed)")
        return original[0](host, *args, **kwargs)

    def _check(address):
        if isinstance(address, (str, bytes)):  # AF_UNIX path
            return
        if not _is_loopback(address[0]):
            raise OutboundNetworkBlocked(f"acceptance: connection to {address[0]!r} blocked (only loopback is allowed)")

    def connect(self, address):
        _check(address)
        return original[1](self, address)

    def connect_ex(self, address):
        _check(address)
        return original[2](self, address)

    def create_connection(address, *args, **kwargs):
        _check(address)
        return original[3](address, *args, **kwargs)

    def sendto(self, data, *args):
        _check(args[-1])  # sendto(data, address) or sendto(data, flags, address)
        return original[4](self, data, *args)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.socket.sendto = sendto

    def uninstall():
        (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, socket.create_connection,
         socket.socket.sendto) = original

    return uninstall

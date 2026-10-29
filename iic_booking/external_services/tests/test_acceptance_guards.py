"""The local acceptance settings fail closed: wrong environment or database, and outbound network access, are refused.

These tests exercise ``config.settings.acceptance_guards`` directly (no database, no acceptance settings loaded). The
network guard is installed only inside each test and removed afterwards.
"""

import imaplib
import smtplib
import socket
import threading
import time

import pytest

from config.settings import acceptance_guards as guards

OK_URL = "postgresql://role@127.0.0.1:5432/iic_extbook_accept_dev"


@pytest.fixture
def blocked():
    uninstall = guards.install_network_guard()
    yield
    uninstall()


# --- environment and database identity --------------------------------------------------------------------------------

def test_environment_must_opt_in_explicitly():
    with pytest.raises(guards.AcceptanceGuardError):
        guards.check_environment({}, path_exists=lambda p: False)
    with pytest.raises(guards.AcceptanceGuardError):
        guards.check_environment({"IIC_ACCEPTANCE_ENV": "production"}, path_exists=lambda p: False)
    guards.check_environment({"IIC_ACCEPTANCE_ENV": "local-acceptance"}, path_exists=lambda p: False)


@pytest.mark.parametrize("env, docker", [
    ({"CI": "true"}, False), ({"GITHUB_ACTIONS": "true"}, False), ({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, False),
    ({}, True),
])
def test_environment_refuses_containers_and_ci(env, docker):
    with pytest.raises(guards.AcceptanceGuardError):
        guards.check_environment({"IIC_ACCEPTANCE_ENV": "local-acceptance", **env}, path_exists=lambda p: docker)


@pytest.mark.parametrize("url", [
    "",
    "mysql://role@127.0.0.1/iic_extbook_accept_dev",
    "postgresql://role@db.example.rds.amazonaws.com:5432/iic_extbook_accept_dev",
    "postgresql://role@10.0.0.5:5432/iic_extbook_accept_dev",
    "postgresql://role@127.0.0.1:5432/iic_extbook",
    "postgresql://role@127.0.0.1:5432/iic_extbook_dev",
    "postgresql://role@127.0.0.1:5432/postgres",
    OK_URL + "?host=10.0.0.5",
])
def test_database_url_must_be_the_local_acceptance_database(url):
    with pytest.raises(guards.AcceptanceGuardError):
        guards.check_database_url(url)


def test_database_url_accepts_only_the_exact_local_target():
    assert guards.check_database_url(OK_URL) == ("127.0.0.1", 5432, "iic_extbook_accept_dev")


def test_connected_database_needs_loopback_and_the_disposable_marker():
    guards.check_connected_database("127.0.0.1", "iic_extbook_accept_dev", guards.DISPOSABLE_MARKER)
    guards.check_connected_database(None, "iic_extbook_accept_dev", guards.DISPOSABLE_MARKER)  # local socket
    for args in (
        ("127.0.0.1", "iic_extbook_accept_dev", None),
        ("127.0.0.1", "iic_extbook_accept_dev", "something else"),
        ("10.1.2.3", "iic_extbook_accept_dev", guards.DISPOSABLE_MARKER),
        ("127.0.0.1", "iic_extbook", guards.DISPOSABLE_MARKER),
    ):
        with pytest.raises(guards.AcceptanceGuardError):
            guards.check_connected_database(*args)


# --- outbound network ---------------------------------------------------------------------------------------------------

@pytest.mark.usefixtures("blocked")
def test_dns_lookup_of_an_external_host_is_blocked():
    with pytest.raises(guards.OutboundNetworkBlocked):
        socket.getaddrinfo("api.razorpay.com", 443)


@pytest.mark.usefixtures("blocked")
def test_connection_to_an_external_ip_is_blocked_without_touching_the_network():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(guards.OutboundNetworkBlocked):
            s.connect(("8.8.8.8", 53))
        with pytest.raises(guards.OutboundNetworkBlocked):
            s.connect_ex(("1.1.1.1", 443))
    finally:
        s.close()
    with pytest.raises(guards.OutboundNetworkBlocked):
        socket.create_connection(("203.0.113.10", 25), timeout=1)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(guards.OutboundNetworkBlocked):
            udp.sendto(b"x", ("8.8.8.8", 53))
        udp.sendto(b"x", ("127.0.0.1", 9))  # loopback datagrams still allowed
    finally:
        udp.close()


@pytest.mark.usefixtures("blocked")
def test_http_clients_used_by_the_app_cannot_reach_external_services():
    import requests

    started = time.monotonic()
    with pytest.raises(requests.exceptions.ConnectionError) as exc:  # Razorpay SDK, web push, Guacamole use requests
        requests.post("https://fcm.googleapis.com/fcm/send/x", data=b"x", timeout=5)
    assert "blocked" in str(exc.value)
    assert time.monotonic() - started < 2  # refused locally, no network wait


@pytest.mark.usefixtures("blocked")
def test_razorpay_sdk_with_synthetic_keys_cannot_reach_the_gateway():
    import razorpay
    import requests

    client = razorpay.Client(auth=("rzp_test_ACCEPTANCELOCAL", "synthetic-secret"))
    with pytest.raises((requests.exceptions.ConnectionError, guards.OutboundNetworkBlocked)):
        client.order.create(data={"amount": 100, "currency": "INR", "receipt": "acceptance"})


@pytest.mark.usefixtures("blocked")
def test_smtp_and_imap_cannot_connect():
    with pytest.raises(guards.OutboundNetworkBlocked):
        smtplib.SMTP("smtp.example.org", 587, timeout=1)
    with pytest.raises(guards.OutboundNetworkBlocked):
        imaplib.IMAP4_SSL("imap.example.org", 993, timeout=1)


@pytest.mark.usefixtures("blocked")
def test_loopback_still_works():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted = []
    t = threading.Thread(target=lambda: accepted.append(server.accept()[0]))
    t.start()
    try:
        client = socket.create_connection(("localhost", port), timeout=5)
        client.close()
        t.join(5)
        assert accepted
    finally:
        for s in accepted:
            s.close()
        server.close()


def test_guard_is_removed_after_uninstall():
    original = socket.getaddrinfo
    uninstall = guards.install_network_guard()
    assert socket.getaddrinfo is not original
    uninstall()
    assert socket.getaddrinfo is original

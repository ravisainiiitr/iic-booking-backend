"""Retired SBIePay return addresses and the SRIC email origin rule for a relay that records no address."""

from __future__ import annotations

import email
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient

from iic_booking.users.sric_wallet_mail_auth import check_message

SENDER = "no-reply@sric.iitr.ac.in"
CONFIG = SimpleNamespace(sender_email=SENDER, trusted_authserv_ids="", require_internal_relay=True, trusted_relay_ranges="",
                         gateway_marker_header="", gateway_marker_value="")


def _message(*hops: str):
    lines = [f"From: {SENDER}", f"Return-Path: <{SENDER}>", *[f"Received: {h}" for h in hops], "", "body"]
    return email.message_from_string("\n".join(lines))


def test_internal_relays_still_pass():
    assert check_message(_message("from a.iitr.ac.in (a.iitr.ac.in [10.1.2.3]) by mail.iitr.ac.in"), CONFIG).authenticated


def test_a_hop_made_on_the_machine_itself_still_passes():
    hops = ("from a.iitr.ac.in (a.iitr.ac.in [10.1.2.3]) by mail.iitr.ac.in", "by a.iitr.ac.in (Postfix, from userid 33)")
    assert check_message(_message(*hops), CONFIG).authenticated


def test_local_delivery_on_the_same_server_still_passes():
    hops = ("from store.iitr.ac.in (store.iitr.ac.in) by store.iitr.ac.in with LMTP", "from a.iitr.ac.in (a.iitr.ac.in [10.1.2.3]) by store.iitr.ac.in")
    assert check_message(_message(*hops), CONFIG).authenticated


def test_a_relay_that_records_no_address_is_not_trusted():
    result = check_message(_message("from outside.example.org by mail.iitr.ac.in"), CONFIG)
    assert not result.authenticated and "no address" in result.verdict


def test_one_such_relay_among_internal_ones_is_not_trusted():
    hops = ("from a.iitr.ac.in (a.iitr.ac.in [10.1.2.3]) by mail.iitr.ac.in", "from outside.example.org by a.iitr.ac.in")
    assert not check_message(_message(*hops), CONFIG).authenticated


@pytest.mark.parametrize("path", ["initiate", "success", "failure", "status"])
@pytest.mark.django_db
def test_sbiepay_addresses_are_gone(path):
    assert APIClient().post(f"/api/payments/sbiepay/{path}/").status_code == 404

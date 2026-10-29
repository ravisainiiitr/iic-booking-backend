import logging

import pytest
import requests

from iic_booking.courier_tracking.client import TrackCourierClient
from iic_booking.courier_tracking.constants import LookupOutcome, ShipmentStatus
from iic_booking.courier_tracking.parsing import (
    WebhookPayloadError,
    normalize_tracking_number,
    parse_track_response,
    parse_webhook_payload,
    tracking_number_problem,
)

from .conftest import FAKE_KEY, FakeResponse, error_payload, track_payload


# --- tracking numbers -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [(" ee 123 456 789 in ", "EE123456789IN"), ("d3010377227", "D3010377227"),
                                          ("7X1188\t22611", "7X118822611")])
def test_normalisation_removes_spaces_and_uppercases(raw, expected):
    assert normalize_tracking_number(raw) == expected


@pytest.mark.parametrize("slug", ["indiapost", "speedpost", "registeredpost", "indiapostinternational"])
def test_postal_numbers_must_match_documented_format(slug):
    assert tracking_number_problem(slug, "EE123456789IN") == ""
    assert "13 characters" in tracking_number_problem(slug, "EE12345678IN")
    assert "13 characters" in tracking_number_problem(slug, "123456789012I")


def test_other_couriers_get_only_generic_bounds():
    assert tracking_number_problem("dtdc", "D3010377227") == ""
    assert tracking_number_problem("bluedart", "1234") != ""  # too short for any courier
    assert tracking_number_problem("bluedart", "") != ""


# --- parsing ----------------------------------------------------------------------------------------------------------

def test_track_response_pascal_case_newest_first():
    parsed = parse_track_response(track_payload("intransit", "In Transit")["data"])
    assert parsed.status == ShipmentStatus.IN_TRANSIT
    assert parsed.provider_state == "intransit" and parsed.status_label == "In Transit"
    assert [c["activity"] for c in parsed.checkpoints] == ["On the way", "Picked up"]
    assert parsed.checkpoints[0]["state"] == "intransit"


@pytest.mark.parametrize("state,expected", [
    ("inforeceived", ShipmentStatus.INFO_RECEIVED), ("pending", ShipmentStatus.PENDING),
    ("outfordelivery", ShipmentStatus.OUT_FOR_DELIVERY), ("attemptfail", ShipmentStatus.ATTEMPT_FAILED),
    ("delivered", ShipmentStatus.DELIVERY_REPORTED), ("exception", ShipmentStatus.EXCEPTION),
    ("expired", ShipmentStatus.EXPIRED),
])
def test_all_documented_states_map(state, expected):
    assert parse_track_response({"ShipmentState": state, "Checkpoints": []}).status == expected


def test_unknown_state_is_kept_raw_without_inventing_a_status():
    parsed = parse_track_response({"ShipmentState": "teleported", "Checkpoints": []})
    assert parsed.status == "" and parsed.provider_state == "teleported"


def test_webhook_snake_case_is_parsed_separately():
    payload = {
        "event": "tracking.delivered", "enqueued_at": "2026-09-18T09:14:22Z",
        "data": {"tracking_number": "7x118822611", "courier": "dtdc", "status": "delivered",
                 "most_recent_status": "Delivered",
                 "checkpoints": [{"activity": "Delivered", "checkpoint_state": "delivered", "courier_name": "DTDC",
                                  "date": "18-Sep-2026", "time": "17:08", "location": "Delhi"}]},
    }
    parsed = parse_webhook_payload(payload)
    assert parsed.tracking_number == "7X118822611" and parsed.courier_slug == "dtdc"
    assert parsed.tracking.status == ShipmentStatus.DELIVERY_REPORTED
    assert parsed.tracking.checkpoints[0]["location"] == "Delhi"
    # A PascalCase body decodes nothing through the snake_case parser.
    assert parse_webhook_payload({"data": {"courier": "dtdc", "tracking_number": "X12345",
                                           "Checkpoints": [{"Activity": "x"}]}}).tracking.checkpoints is None


def test_webhook_null_checkpoints_means_keep_existing():
    parsed = parse_webhook_payload({"event": "tracking.updated", "data": {"courier": "dtdc", "tracking_number": "ABC12345",
                                                                         "status": "in_transit", "checkpoints": None}})
    assert parsed.tracking.checkpoints is None and parsed.tracking.status == ShipmentStatus.IN_TRANSIT


@pytest.mark.parametrize("status,expected", [("returned", ShipmentStatus.RETURNED), ("cancelled", ShipmentStatus.CANCELLED),
                                             ("exception", ShipmentStatus.EXCEPTION)])
def test_webhook_exception_family(status, expected):
    parsed = parse_webhook_payload({"event": "tracking.exception", "data": {"courier": "dtdc", "tracking_number": "ABC12345",
                                                                           "status": status, "checkpoints": []}})
    assert parsed.tracking.status == expected


def test_malformed_webhook_payload_rejected():
    with pytest.raises(WebhookPayloadError):
        parse_webhook_payload({"event": "tracking.updated", "data": {"courier": "dtdc"}})


# --- client -----------------------------------------------------------------------------------------------------------

def test_client_sends_key_header_https_timeout_and_reads_usage(provider):
    provider.queue(FakeResponse(200, track_payload(used=7, quota=100)))
    result = TrackCourierClient().track("dtdc", "D3010377227")
    assert result.ok and result.usage.used == 7 and result.usage.quota == 100 and result.usage.plan == "free"
    call = provider.track_calls[0]
    assert call["headers"]["X-API-Key"] == FAKE_KEY and call["url"].startswith("https://")
    assert call["params"] == {"courier": "dtdc", "tracking_number": "D3010377227"} and call["timeout"]


@pytest.mark.parametrize("code,http,outcome", [
    ("API_KEY_REQUIRED", 401, LookupOutcome.AUTH_ERROR), ("INVALID_API_KEY", 401, LookupOutcome.AUTH_ERROR),
    ("QUOTA_EXCEEDED", 402, LookupOutcome.QUOTA_EXCEEDED),
    ("ACCOUNT_NEEDS_ATTENTION", 402, LookupOutcome.ACCOUNT_NEEDS_ATTENTION),
    ("COURIER_NOT_FOUND", 404, LookupOutcome.COURIER_NOT_FOUND),
    ("INVALID_TRACKING_NUMBER", 400, LookupOutcome.INVALID_TRACKING_NUMBER),
    ("COURIER_NOT_SUPPORTED", 400, LookupOutcome.COURIER_NOT_SUPPORTED),
    ("TRACKING_ERROR", 422, LookupOutcome.TRACKING_ERROR), ("TIMEOUT", 504, LookupOutcome.TIMEOUT),
    ("SERVICE_ERROR", 404, LookupOutcome.SERVICE_ERROR),  # documented as 5xx *or 404*: code wins over status
])
def test_error_code_not_http_status_decides(provider, code, http, outcome):
    provider.queue(FakeResponse(http, error_payload(code, used=100, quota=100)))
    result = TrackCourierClient().track("dtdc", "D3010377227")
    assert not result.ok and result.outcome == outcome and result.error_code == code
    assert result.usage.used == 100


@pytest.mark.parametrize("failure,outcome", [(requests.Timeout(), LookupOutcome.TIMEOUT),
                                             (requests.ConnectionError(), LookupOutcome.NETWORK_ERROR)])
def test_transport_failures(provider, failure, outcome):
    provider.queue(failure)
    assert TrackCourierClient().track("dtdc", "D3010377227").outcome == outcome


def test_non_json_5xx_is_service_error(provider):
    provider.queue(FakeResponse(502, None))
    assert TrackCourierClient().track("dtdc", "D3010377227").outcome == LookupOutcome.SERVICE_ERROR


def test_no_key_means_no_request(settings, provider):
    settings.TRACKCOURIER_API_KEY = ""
    result = TrackCourierClient().track("dtdc", "D3010377227")
    assert result.outcome == LookupOutcome.AUTH_ERROR and provider.calls == []


def test_logs_carry_no_key_url_or_full_number(provider, caplog):
    provider.queue(FakeResponse(200, track_payload()))
    with caplog.at_level(logging.INFO, logger="iic_booking.courier_tracking.client"):
        TrackCourierClient().track("dtdc", "D3010377227")
    text = caplog.text
    assert FAKE_KEY not in text and "D3010377227" not in text and "tracking_number=" not in text
    assert "...7227" in text


def test_couriers_list_is_validated(provider):
    result = TrackCourierClient().list_couriers()
    assert result.ok and {"slug": "bluedart", "name": "Blue Dart"} in result.data
    provider.couriers_response = FakeResponse(200, {"success": True, "data": {"couriers": "nope"}})
    assert TrackCourierClient().list_couriers().outcome == LookupOutcome.MALFORMED_RESPONSE

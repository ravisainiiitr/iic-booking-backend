"""I-STEM HTTP client against a scripted fake transport: authentication, token reuse, response parsing and error
classification. No request leaves the process."""

from __future__ import annotations

import logging

import pytest

from iic_booking.istem import contract
from iic_booking.istem.choices import ErrorCategory
from iic_booking.istem.client import IstemClient, RequestsTransport, clear_token_cache
from iic_booking.istem.errors import IstemConfigError, IstemError

from .support import (
    BASE_URL, FAKE_PASSWORD, FAKE_TOKEN, FAKE_USERNAME, NOT_FOUND, FakeTransport, apply_live, json_response, listing_response,
    listing_row, staging_token_response, token_response,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def live(settings):
    apply_live(settings)
    return settings


def _client(transport, clock=None):
    return IstemClient(transport, clock=clock or Clock())


BOOKING_OK = {"status": "Sucess", "message": "Done", "file_path": "x.pdf", "new_service_id": 55401, "fbr_no": "1976125722"}


# --- refusal outside live mode ----------------------------------------------------------------------------------

@pytest.mark.parametrize("enabled,mode", [(False, "live"), (True, "off"), (True, "shadow"), (True, "bogus")])
def test_client_refuses_outside_live_mode(settings, enabled, mode):
    apply_live(settings, ISTEM_ENABLED=enabled, ISTEM_MODE=mode)
    with pytest.raises(IstemConfigError):
        IstemClient(FakeTransport())


@pytest.mark.parametrize("missing", ["ISTEM_BASE_URL", "ISTEM_USERNAME", "ISTEM_PASSWORD"])
def test_client_refuses_without_connection_settings(settings, missing):
    apply_live(settings, **{missing: ""})
    with pytest.raises(IstemConfigError) as exc:
        IstemClient(FakeTransport())
    assert missing in exc.value.message
    assert FAKE_PASSWORD not in exc.value.message


# --- authentication ----------------------------------------------------------------------------------------------

def test_token_is_requested_with_form_fields_and_sent_as_token_header(live):
    transport = FakeTransport(token_response(), json_response(BOOKING_OK))
    _client(transport).create_booking({"equipment_id": "1"})

    auth, call = transport.calls
    assert auth["url"] == BASE_URL + contract.AUTH_TOKEN_PATH
    assert auth["method"] == "POST"
    assert auth["data"] == {"username": FAKE_USERNAME, "password": FAKE_PASSWORD}
    assert auth["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert call["url"] == BASE_URL + contract.CREATE_BOOKING_PATH
    assert call["headers"][contract.TOKEN_HEADER] == FAKE_TOKEN
    assert "Authorization" not in call["headers"]


def test_token_is_reused_within_its_lifetime(live):
    clock = Clock()
    transport = FakeTransport(token_response(), json_response(BOOKING_OK), json_response(BOOKING_OK))
    client = _client(transport, clock)
    client.create_booking({})
    clock.now += 3000  # still inside 3600 - 300 s margin
    client.create_booking({})
    assert transport.paths() == [contract.AUTH_TOKEN_PATH, contract.CREATE_BOOKING_PATH, contract.CREATE_BOOKING_PATH]


def test_token_is_shared_between_client_instances(live):
    transport = FakeTransport(token_response(), json_response(BOOKING_OK), json_response(BOOKING_OK))
    clock = Clock()
    _client(transport, clock).create_booking({})
    _client(transport, clock).create_booking({})
    assert transport.paths().count(contract.AUTH_TOKEN_PATH) == 1


def test_token_is_renewed_before_the_documented_expiry(live):
    clock = Clock()
    transport = FakeTransport(token_response("t1"), json_response(BOOKING_OK), token_response("t2"), json_response(BOOKING_OK))
    client = _client(transport, clock)
    client.create_booking({})
    clock.now += 3301  # past expires_in minus the 300 s margin
    client.create_booking({})
    assert [c["headers"].get(contract.TOKEN_HEADER) for c in transport.calls if "create-booking" in c["url"]] == ["t1", "t2"]


def test_401_on_a_write_is_repeated_once_with_a_fresh_token(live):
    # A refused request was not processed (and Create Booking is idempotent), so one repeat with a new token is safe.
    transport = FakeTransport(token_response("t1"), json_response({}, status=401), token_response("t2"), json_response(BOOKING_OK))
    assert _client(transport).create_booking({}).request_id == "55401"
    assert [c["headers"].get(contract.TOKEN_HEADER) for c in transport.calls if "create-booking" in c["url"]] == ["t1", "t2"]


def test_write_refused_twice_is_an_auth_error_and_drops_the_token(live):
    transport = FakeTransport(token_response("t1"), json_response({}, status=401), token_response("t2"), json_response({}, status=401))
    client = _client(transport)
    with pytest.raises(IstemError) as exc:
        client.create_booking({})
    assert exc.value.category == ErrorCategory.AUTH and exc.value.auth_failure
    assert transport.paths().count(contract.CREATE_BOOKING_PATH) == 2
    transport.responses += [token_response("t3"), json_response(BOOKING_OK)]
    client.create_booking({})
    assert transport.paths()[-2:] == [contract.AUTH_TOKEN_PATH, contract.CREATE_BOOKING_PATH]


def test_documented_token_error_body_is_treated_as_an_expired_token(live):
    # [SPEC] a missing/invalid token is answered with HTTP 200 {"status": "ERROR", "message": "Token not found"}.
    refused = json_response({"status": "ERROR", "message": "Token not found"})
    transport = FakeTransport(token_response("t1"), refused, token_response("t2"), json_response(BOOKING_OK))
    assert _client(transport).create_booking({}).request_id == "55401"
    clear_token_cache()
    transport = FakeTransport(token_response("t3"), refused, token_response("t4"), refused)
    with pytest.raises(IstemError) as exc:
        _client(transport).create_booking({})
    assert exc.value.category == ErrorCategory.AUTH  # not a definite business refusal of the booking


def test_session_info_comes_from_the_token_response(live):
    transport = FakeTransport(token_response(institute_id="213"))
    info = _client(transport).session_info()
    assert (info.institute_id, info.user_id, info.user_entity) == ("213", "2740", "IN")
    assert FAKE_TOKEN not in repr(info)
    assert _client(FakeTransport()).session_info().institute_id == "213"  # cached with the token


def test_wrong_credentials_answered_with_http_200_are_an_auth_error(live):
    # [SPEC] invalid_credentials example: HTTP 200 {"result": "Error", "message": "Invalid Login Credentials"}
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(json_response({"result": "Error", "message": "Invalid Login Credentials"}))).session_info()
    assert exc.value.category == ErrorCategory.AUTH


def test_401_on_a_read_is_retried_once_with_a_fresh_token(live):
    ok = {"status": "SUCCESS", "message": "Booking get successfully", "result": []}
    transport = FakeTransport(token_response("t1"), json_response({}, status=401), token_response("t2"), json_response(ok))
    assert _client(transport).get_bookings(fbr_no="1") == []
    assert [c["headers"].get(contract.TOKEN_HEADER) for c in transport.calls if "get-bookings" in c["url"]] == ["t1", "t2"]


@pytest.mark.parametrize("response", [
    json_response({}, status=401),
    json_response({"result": "SUCCESS", "message": {"token_type": "Bearer"}}),
    json_response({"result": "FAILED", "message": "Invalid credentials"}),
])
def test_rejected_or_tokenless_authentication_is_an_auth_error(live, response):
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(response)).create_booking({})
    assert exc.value.category == ErrorCategory.AUTH


def test_unreachable_auth_endpoint_is_not_sent(live):
    transport = FakeTransport(IstemError(ErrorCategory.CONNECT_FAILED, "down"))
    with pytest.raises(IstemError) as exc:
        _client(transport).create_booking({})
    assert exc.value.category == ErrorCategory.AUTH_UNAVAILABLE
    assert exc.value.not_sent
    assert transport.paths() == [contract.AUTH_TOKEN_PATH]


def test_logs_never_contain_credentials_tokens_or_bodies(live, caplog):
    caplog.set_level(logging.DEBUG, logger="iic_booking.istem")
    transport = FakeTransport(token_response(), json_response({"status": False, "message": "This slot is already booked."}))
    with pytest.raises(IstemError):
        _client(transport).create_booking({"billing_user_name": "Secret Person"})
    text = caplog.text
    for secret in (FAKE_PASSWORD, FAKE_USERNAME, FAKE_TOKEN, "Secret Person"):
        assert secret not in text
    assert "op=create_booking" in text


# --- Create Booking ----------------------------------------------------------------------------------------------

def test_create_booking_success_returns_service_id_and_fbr(live):
    result = _client(FakeTransport(token_response(), json_response(BOOKING_OK))).create_booking({})
    assert (result.request_id, result.fbr_no) == ("55401", "1976125722")


def test_create_booking_without_fbr_no_reads_it_from_the_pending_listing(live):
    # [STAGING] Success carries no fbr_no; the client reads it from Get All Bookings ("Pending").
    body = {"status": "Sucess", "message": "Done", "file_path": "x.pdf", "new_service_id": 95535}
    transport = FakeTransport(token_response(), json_response(body),
                              listing_response(listing_row(95536), listing_row(95535), listing_row(95534)))
    result = _client(transport).create_booking({})
    assert (result.request_id, result.fbr_no) == ("95535", "1976095535")
    assert transport.calls[-1]["params"]["status_code"] == "Pending"


def test_create_booking_without_fbr_no_stays_created_when_the_listing_misses(live):
    body = {"status": "Sucess", "message": "Done", "file_path": "x.pdf", "new_service_id": 55401}
    # Newest first: the page already holds older requests only, so the lookup stops without reading further.
    transport = FakeTransport(token_response(), json_response(body), listing_response(*[listing_row(55400 - i) for i in range(100)]))
    result = _client(transport).create_booking({})
    assert (result.request_id, result.fbr_no) == ("55401", None)
    assert len(transport.calls) == 3


def test_create_booking_new_service_id_zero_is_not_a_booking(live):
    # [STAGING] Without booking_remark: "Sucess" with new_service_id 0 and nothing created.
    body = {"status": "Sucess", "message": "Done", "file_path": "x", "new_service_id": 0}
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response(body))).create_booking({})
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_create_booking_business_refusal_is_definite(live):
    body = {"status": False, "message": "This slot is already booked."}
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response(body))).create_booking({})
    assert exc.value.category == ErrorCategory.BUSINESS_ERROR
    assert exc.value.definite_failure
    assert "already booked" in exc.value.message


@pytest.mark.parametrize("body", [
    {"status": "Sucess", "message": "Done"},
    {"status": "Sucess", "new_service_id": "abc"},
    {"status": "Sucess", "new_service_id": True},
    {"message": "Done", "new_service_id": 55401},
])
def test_create_booking_success_without_a_valid_id_is_uncertain(live, body):
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response(body))).create_booking({})
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE
    assert exc.value.possibly_processed


@pytest.mark.parametrize("status,category,possibly_processed", [
    (500, ErrorCategory.SERVER_ERROR, True),
    (502, ErrorCategory.SERVER_ERROR, True),
    (429, ErrorCategory.RATE_LIMITED, True),
    (403, ErrorCategory.FORBIDDEN, False),
    (404, ErrorCategory.NOT_FOUND, False),
    (422, ErrorCategory.VALIDATION, False),
    (302, ErrorCategory.INVALID_RESPONSE, True),
])
def test_http_status_classification(live, status, category, possibly_processed):
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response({}, status=status))).create_booking({})
    assert exc.value.category == category
    assert exc.value.possibly_processed is possibly_processed


def test_non_json_body_is_uncertain(live):
    from iic_booking.istem.client import TransportResponse

    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), TransportResponse(200, "<html>oops</html>"))).create_booking({})
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


@pytest.mark.parametrize("category", [ErrorCategory.READ_TIMEOUT, ErrorCategory.CONNECTION_LOST])
def test_transport_failures_after_sending_are_uncertain_and_not_repeated(live, category):
    transport = FakeTransport(token_response(), IstemError(category, "lost"))
    with pytest.raises(IstemError) as exc:
        _client(transport).create_booking({})
    assert exc.value.possibly_processed
    assert transport.paths().count(contract.CREATE_BOOKING_PATH) == 1


# --- Create User -------------------------------------------------------------------------------------------------

def test_create_user_success(live):
    body = {"status": "SUCCESS", "message": "Public user created successfully.", "user_id": 35151}
    result = _client(FakeTransport(token_response(), json_response(body))).create_user({})
    assert (result.created, result.user_id) == (True, "35151")


@pytest.mark.parametrize("body,user_id", [
    ({"status": False, "message": "User already exists with this email", "user_id": 777}, "777"),
    ({"status": "FAILED", "message": "Mobile number already registered"}, None),
    ({"status": False, "message": "User already exists", "user_id": "not-a-number"}, None),
])
def test_create_user_duplicate_response_is_reported_not_trusted_blindly(live, body, user_id):
    result = _client(FakeTransport(token_response(), json_response(body))).create_user({})
    assert result.created is False
    assert result.user_id == user_id


def test_create_user_other_refusal_is_business_error(live):
    body = {"status": False, "message": "Invalid institute"}
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response(body))).create_user({})
    assert exc.value.category == ErrorCategory.BUSINESS_ERROR


def test_create_user_success_without_user_id_is_invalid(live):
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response({"status": "SUCCESS"}))).create_user({})
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


# --- Confirm / reads ---------------------------------------------------------------------------------------------

def test_confirm_requires_the_echoed_request_id(live):
    body = {"status": "SUCCESS", "result": {"request_id": "99999"}}
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response(body))).confirm_booking({}, "55401")
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_confirm_success_without_confirmation_id_is_uncertain(live):
    # [SPEC] the success response carries confirmation_id; the older SOP sample did not. Without it, and without the
    # booking in the "Slot Allocated" listing, nothing is final.
    body = {"status": "SUCCESS", "message": "Booking confirmed", "result": {"request_id": "55401", "approved_by": "x"}}
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response(body), listing_response())).confirm_booking({}, "55401")
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE and exc.value.possibly_processed


def test_confirm_staging_answer_takes_the_confirmation_id_from_the_listing(live):
    # [STAGING] "Allocated sucessfully" with a result but no confirmation_id; Get All Bookings has it.
    body = {"status": "Sucess", "message": "Allocated sucessfully",
            "result": {"request_id": "95554", "confirmation_date": "2026-10-21 10:00:00", "booking_status": "Slot Allocated"}}
    transport = FakeTransport(token_response(), json_response(body),
                              listing_response(listing_row(95554, confirmation_id=71981, status_code="Slot Allocated")))
    result = _client(transport).confirm_booking({}, "95554")
    assert (result.request_id, result.confirmation_id, result.already_confirmed) == ("95554", "71981", False)
    assert transport.calls[-1]["params"]["status_code"] == "Slot Allocated"


def test_confirm_spec_success_and_already_confirmed(live):
    ok = {"status": "Sucess", "message": "Booking Confirmed sucessfully", "confirmation_id": 54210,
          "result": {"request_id": 126853, "confirmation_date": "2025-10-21 14:00:00"}}
    result = _client(FakeTransport(token_response(), json_response(ok))).confirm_booking({}, "126853")
    assert (result.request_id, result.confirmation_id, result.already_confirmed) == ("126853", "54210", False)
    again = {"status": "Sucess", "message": "Booking is already confirmed", "confirmation_id": 54210, "result": {}}
    result = _client(FakeTransport(json_response(again))).confirm_booking({}, "126853")
    assert (result.confirmation_id, result.already_confirmed) == ("54210", True)


def test_confirm_refusal_is_definite(live):
    body = {"status": "ERROR", "message": "Public user not have the access for this module."}
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response(body))).confirm_booking({}, "55401")
    assert exc.value.category == ErrorCategory.BUSINESS_ERROR and exc.value.definite_failure


# --- FBR Execute -------------------------------------------------------------------------------------------------

EXECUTE_PAYLOAD = {"request_id": "126853", "confirmation_id": "54210", "completion_date": "2025-10-22",
                   "allocation_date": "2025-10-21", "final_amount": "1014.50"}


@pytest.mark.parametrize("body,already", [
    ({"status": "SUCCESS", "message": "FBR is Marked as Executed successfully", "payment_id": 9981,
      "request_id": 126853, "confirmation_id": 54210}, False),
    ({"status": "SUCCESS", "message": "FBR was already marked as Executed", "payment_id": 9981,
      "request_id": "126853", "confirmation_id": "54210"}, True),
])
def test_execute_spec_responses(live, body, already):
    result = _client(FakeTransport(token_response(), json_response(body))).execute_fbr(EXECUTE_PAYLOAD)
    assert (result.payment_id, result.request_id, result.confirmation_id, result.already_executed) == ("9981", "126853", "54210", already)


@pytest.mark.parametrize("body", [
    {"status": "SUCCESS", "payment_id": 9981, "request_id": 126853, "confirmation_id": 1},
    {"status": "SUCCESS", "payment_id": 9981, "request_id": 1, "confirmation_id": 54210},
    {"status": "SUCCESS", "request_id": 126853, "confirmation_id": 54210},
    {"status": "SUCCESS", "message": "FBR executed"},  # the earlier undocumented shape
])
def test_execute_success_that_cannot_be_matched_is_uncertain(live, body):
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response(body))).execute_fbr(EXECUTE_PAYLOAD)
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE and exc.value.possibly_processed


@pytest.mark.parametrize("message", ["Invalid confirmation_id", "confirmation_id does not belong to the given request_id"])
def test_execute_documented_refusals_are_definite(live, message):
    with pytest.raises(IstemError) as exc:
        _client(FakeTransport(token_response(), json_response({"status": "ERROR", "message": message}))).execute_fbr(EXECUTE_PAYLOAD)
    assert exc.value.definite_failure and message in exc.value.message


# --- read-only lookups -------------------------------------------------------------------------------------------

def test_equipment_list_reads_every_page_for_the_institute(live, monkeypatch):
    from iic_booking.istem import client as client_module

    monkeypatch.setattr(client_module, "EQUIPMENT_PAGE_SIZE", 2)
    page = lambda ids, total=3: json_response({"status": "SUCCESS", "message": "Equipment Found", "totalrecord": total,
                                               "records": len(ids), "result": [{"equipment_id": i, "equipment_name": f"E{i}"} for i in ids]})
    transport = FakeTransport(token_response(), page([29907, 45671]), page([12206]))
    rows = _client(transport).list_all_equipment(institute_id="213")
    assert [r["equipment_id"] for r in rows] == ["29907", "45671", "12206"]
    assert [c["params"] for c in transport.calls[1:]] == [
        {"page": 1, "limit": 2, "institute_id": "213"}, {"page": 2, "limit": 2, "institute_id": "213"},
    ]
    assert all(c["method"] == "GET" for c in transport.calls[1:])


def test_equipment_list_no_results_is_empty(live):
    body = {"status": "ERROR", "message": "No equipment found", "result": []}
    assert _client(FakeTransport(token_response(), json_response(body))).list_all_equipment(institute_id="213") == []


def test_equipment_list_unexpected_answer_raises(live):
    with pytest.raises(IstemError):
        _client(FakeTransport(token_response(), json_response({"status": "ERROR", "message": "Server busy"}))).list_all_equipment()


@pytest.mark.parametrize("body,expected", [
    ({"status": "SUCCESS", "message": "User found", "result": {"user_id": 22798, "user_name": "PU_X"}}, ["22798"]),
    ({"status": "SUCCESS", "message": "Multiple matching users found", "result": [{"user_id": 1}, {"user_id": 2}]}, ["1", "2"]),
    ({"status": "ERROR", "message": "No matching user found"}, []),
])
def test_public_user_lookup(live, body, expected):
    transport = FakeTransport(token_response(), json_response(body))
    rows = _client(transport).lookup_public_user(user_email="a@b.in", user_contactno="9876543210")
    assert [r["user_id"] for r in rows] == expected
    assert transport.calls[1]["method"] == "GET" and transport.calls[1]["params"] == {"user_email": "a@b.in", "user_contactno": "9876543210"}


def test_get_fbr_details_not_found_is_empty(live):
    # Not found by Get FBR Details nor by Get Bookings under any status.
    misses = [json_response(NOT_FOUND) for _ in contract.REMOTE_STATUS_CODES]
    assert _client(FakeTransport(token_response(), json_response(NOT_FOUND), *misses)).get_fbr_details("1") == []


def test_get_fbr_details_falls_back_to_get_bookings_by_status(live):
    # [STAGING] Get FBR Details never finds a booking; Get Bookings with the right status does.
    executed = {"status": "SUCCESS", "message": "Booking get successfully", "totalrecord": 1, "records": 1,
                "result": [{"request_id": "95554", "fbr_no": "1976095554", "fbr_status_code": "Executed"}]}
    transport = FakeTransport(token_response(), json_response(NOT_FOUND), json_response(NOT_FOUND),
                              json_response(NOT_FOUND), json_response(executed))
    assert _client(transport).get_fbr_details("1976095554") == [{"fbr_no": "1976095554", "status_code": "Executed"}]
    assert [c["params"].get("status_code") for c in transport.calls[2:]] == ["Pending", "Slot Allocated", "Executed"]


def test_get_bookings_slot_allocated_rows_get_their_confirmation_id(live):
    # [STAGING] Get Bookings carries no confirmation_id; it is added from Get All Bookings.
    row = {"request_id": "95543", "fbr_no": "1976095543", "fbr_status_code": "Slot Allocated"}
    transport = FakeTransport(token_response(), json_response({"status": "SUCCESS", "result": [row]}),
                              listing_response(listing_row(95543, confirmation_id=71980, status_code="Slot Allocated")))
    rows = _client(transport).get_bookings(fbr_no="1976095543", status_code="Slot Allocated")
    assert rows[0]["confirmation_id"] == "71980"


def test_listing_lookup_reads_further_pages_until_it_passes_the_request(live):
    page1 = listing_response(*[listing_row(95600 - i) for i in range(100)], total=250)
    page2 = listing_response(listing_row(95480, confirmation_id=71900, status_code="Slot Allocated"))
    transport = FakeTransport(token_response(), page1, page2)
    found = _client(transport).find_listed_booking("95480", "Slot Allocated")
    assert found == {"request_id": "95480", "confirmation_id": "71900", "fbr_no": "1976095480", "status_code": "Slot Allocated"}
    assert [c["params"]["page"] for c in transport.calls[1:]] == [1, 2]


# --- [STAGING] account and catalogue -------------------------------------------------------------------------------

def test_staging_token_without_institute_uses_the_configured_one(live, settings):
    settings.ISTEM_INSTITUTE_ID = "213"
    assert _client(FakeTransport(staging_token_response())).session_info().institute_id == "213"


def test_staging_token_without_institute_and_no_setting_has_none(live, settings):
    settings.ISTEM_INSTITUTE_ID = ""
    assert _client(FakeTransport(staging_token_response())).session_info().institute_id is None


def test_configured_institute_contradicting_the_token_is_refused(live, settings):
    settings.ISTEM_INSTITUTE_ID = "999"
    with pytest.raises(IstemConfigError):
        _client(FakeTransport(token_response())).session_info()


def test_duplicate_user_takes_the_existing_id_from_data(live):
    # [STAGING] {"result": "ERROR", "message": "Mobile number already exists", "data": [{"user_id": "60016", ...}]}
    body = {"result": "ERROR", "message": "Mobile number already exists",
            "data": [{"user_id": "60016", "user_name": "PU_X 001", "user_status": "1"}]}
    result = _client(FakeTransport(token_response(), json_response(body))).create_user({})
    assert (result.created, result.user_id) == (False, "60016")
    assert (result.email, result.matched_on) == (None, "mobile")  # [STAGING] the row carries no email


@pytest.mark.parametrize("body,email,matched_on", [
    ({"result": "ERROR", "message": "EmailId already exists", "data": [{"user_id": "60016", "user_email": "A@Example.com"}]},
     "a@example.com", "email"),
    ({"status": False, "message": "User already exists", "user_id": 777, "email": "b@example.com"}, "b@example.com", ""),
    ({"result": "ERROR", "message": "EmailId already exists", "data": [{"user_id": "60016", "user_email": "not-an-email"}]},
     None, "email"),
    ({"status": False, "message": "Mobile number already registered", "email": "c@example.com"}, None, "mobile"),
])
def test_duplicate_user_reports_the_registered_email_and_matched_field(live, body, email, matched_on):
    result = _client(FakeTransport(token_response(), json_response(body))).create_user({})
    assert (result.email, result.matched_on) == (email, matched_on)


def test_equipment_of_other_institutes_is_dropped(live):
    # [STAGING] the institute_id parameter is ignored: every institute's equipment comes back.
    rows = [{"equipment_id": "12454", "equipment_name": "VSM - IIC", "institute_id": "213"},
            {"equipment_id": "7943", "equipment_name": "ICPMS", "institute_id": "235"}]
    body = {"status": "SUCCESS", "message": "Equipment Found", "totalrecord": 2, "records": 2, "result": rows}
    page = _client(FakeTransport(token_response(), json_response(body))).list_equipment(institute_id="213")
    assert page.rows == [{"equipment_id": "12454", "equipment_name": "VSM - IIC"}]
    assert page.raw_count == 2


def test_cancel_booking_staging_answer(live):
    body = {"status": "Sucess", "message": "Done", "file_path": "cancelled Sucessfully"}
    transport = FakeTransport(token_response(), json_response(body))
    assert _client(transport).cancel_booking("95537", cancelled_on="2026-10-29 00:00:00", remarks="r", cancelled_by="X") == "Done"
    assert transport.calls[-1]["data"]["cancelled"] == "true"


def test_reject_spelling_sucess_is_success(live):
    from iic_booking.istem.client import _is_success

    assert _is_success({"status": "SUCESS"}) is True


def test_get_fbr_details_parses_rows(live):
    body = {"status": "SUCCESS", "result": [{"fbr_no": "1976125722", "status_code": "Pending"}, "junk"]}
    transport = FakeTransport(token_response(), json_response(body))
    rows = _client(transport).get_fbr_details("1976125722")
    assert rows == [{"fbr_no": "1976125722", "status_code": "Pending"}]
    assert transport.calls[-1]["method"] == "GET"
    assert transport.calls[-1]["params"] == {"fbr_no": "1976125722"}


# --- requests transport ------------------------------------------------------------------------------------------

def test_requests_transport_maps_exceptions_without_retrying():
    import requests

    class Session(requests.Session):
        def __init__(self, exc):
            super().__init__()
            self.exc, self.count = exc, 0

        def request(self, *args, **kwargs):
            self.count += 1
            assert kwargs["allow_redirects"] is False
            assert kwargs["timeout"] == (5, 30)
            raise self.exc

    for exc, category in (
        (requests.exceptions.ConnectTimeout(), ErrorCategory.CONNECT_FAILED),
        (requests.exceptions.SSLError(), ErrorCategory.TLS),
        (requests.exceptions.ReadTimeout(), ErrorCategory.READ_TIMEOUT),
        (requests.exceptions.ConnectionError(), ErrorCategory.CONNECTION_LOST),
    ):
        session = Session(exc)
        with pytest.raises(IstemError) as err:
            RequestsTransport(session).send("POST", "https://istem.invalid/x", headers={}, timeout=(5, 30))
        assert err.value.category == category
        assert session.count == 1


def test_transport_verifies_with_the_configured_ca_bundle(settings):
    settings.ISTEM_CA_BUNDLE = "/etc/istem/ca.pem"
    assert RequestsTransport().session.verify == "/etc/istem/ca.pem"
    settings.ISTEM_CA_BUNDLE = ""
    assert RequestsTransport().session.verify is True  # never disabled

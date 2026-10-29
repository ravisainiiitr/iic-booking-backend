"""The I-STEM API contract as documented, in one place.

Sources, most authoritative first:
- [SPEC] OpenAPI 3.0.3 "I-STEM" v1.1.0, ``https://www.istem.gov.in/developer/api-doc-ordered-with-auth.yaml``
  (read 2026-10-26; "generated from, and verified against, the live controller code"). Where it disagrees with an
  older source, the spec is followed and the difference is noted next to the value.
- [TEAM] I-STEM's written answers (``response from istem.txt``) and their October 2026 clarifications.
- [SOP] ``SOP FOR I-STEM API.docx`` and ``API Developer Documentation.pdf`` (older; sample requests).
- [STAGING] Calls made against ``https://istemstaging.iisc.ac.in/istem1/`` on 2026-10-28 with the IIT Roorkee
  developer account (report: ``D:\\IIC_NEW\\istem-staging-api-verification-2026-10-28.md``). Staging runs an older
  build than [SPEC]; where they differ the client accepts both, preferring the [SPEC] field when it is present.
"""

from zoneinfo import ZoneInfo

# --- Endpoints (paths are relative to ISTEM_BASE_URL) ---------------------------------------------------------------
# [SPEC] server https://istem.acoding.com/ ; [SOP] staging https://istemstaging.iisc.ac.in/istem1/
AUTH_TOKEN_PATH = "auth/token"  # POST, form: username, password
CREATE_USER_PATH = "api/public-user"  # POST, form
PUBLIC_USER_LOOKUP_PATH = "api/public-user-lookup"  # GET, query: user_email and/or user_contactno (read-only)
EQUIPMENT_LIST_PATH = "api/equipment-list"  # GET, query: institute_id, page, limit, ... (read-only, Active only)
CREATE_BOOKING_PATH = "api/create-booking"  # POST, form
CONFIRM_BOOKING_PATH = "api/confirm-booking"  # POST, form
GET_BOOKINGS_PATH = "api/get-bookings"  # GET, query: fbr_no + status_code (both required)
GET_FBR_DETAILS_PATH = "api/get-fbr-details"  # GET, query: fbr_no
FBR_EXECUTE_PATH = "api/fbr-execute"  # POST, form
# GET, query: status_code (required) + page, limit, from_date, to_date (DD-Mon-YYYY). [STAGING] The custodian's
# bookings for one status, newest request first; rows carry book_req_id, confirmation_id and fbc_no (the FBR number).
GET_ALL_BOOKINGS_PATH = "api/get-all-bookings"
# POST, form: request_id, cancelled_date, remarks, cancelled ("true"), cancelled_by. [STAGING] verified (by request_id).
CANCEL_BOOKING_PATH = "api/cancel-booking"

# [STAGING] Not available there: public-user-lookup answers HTTP 404 (HTML), get-fbr-details answers "booking not
# found" for every FBR (including bookings it has just created), reschedule-booking crashes with HTTP 500 once its
# validation passes. The client falls back to Get Bookings / Get All Bookings where it can.

# [SPEC] securitySchemes.TokenAuth: apiKey in header "Token".
TOKEN_HEADER = "Token"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

# [SPEC] /auth/token success: {"result": "SUCCESS", "message": {"access_token", "token_type", "expires_in": 3600,
# "user_id", "user_entity", "institute_id"}}. Wrong credentials are HTTP 200 with {"result": "Error"}.
DOCUMENTED_TOKEN_LIFETIME_SECONDS = 3600

# [STAGING] The token message carries only access_token, token_type ("Bearer") and expires_in: no institute_id,
# user_id or user_entity. The institute then comes from ISTEM_INSTITUTE_ID (IIT Roorkee is 213); when the token does
# name an institute, it wins and a different configured value is refused.
# [STAGING] Only the "Token" header is accepted; "Authorization: Bearer" and no header answer 401 "Invalid Token".

# Success spellings that appear in the documented responses ("SUCCESS", "Sucess", "success") and on [STAGING]
# ("SUCESS" from reject-booking, which lower-cases to "sucess"). Errors arrive as HTTP 200 with status/result
# "ERROR" or false; the status key is "status" on most endpoints and "result" on Create/Edit User.
SUCCESS_STATUS_VALUES = frozenset({"success", "sucess"})

# --- Dates ----------------------------------------------------------------------------------------------------------
# [TEAM] All dates are IST. Formatting always converts to this zone explicitly.
IST = ZoneInfo("Asia/Kolkata")
# [SPEC] Create/Confirm Booking examples: "2025-10-18 14:00:00". DISCREPANCY: the older [SOP] samples append ".000";
# the spec is followed.
BOOKING_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# [SPEC] FBR Execute completion_date / allocation_date: "Format YYYY-MM-DD". DISCREPANCY: the earlier [TEAM] answer
# said "YYYY-MM-DD HH:MM:SS"; the spec and the October 2026 clarification both say YYYY-MM-DD.
# [STAGING] YYYY-MM-DD and the staging spec's DD/MM/YYYY are both accepted.
FBR_EXECUTE_DATE_FORMAT = "%Y-%m-%d"
# [STAGING] Get All Bookings from_date / to_date. Rows report request_date / confirmation_date in the same form.
LISTING_DATE_FORMAT = "%d-%b-%Y"

# --- Accepted values supplied by I-STEM -----------------------------------------------------------------------------
# Create User ``user_type`` (different list from Create Booking).
PUBLIC_USER_TYPES = (
    "Industry User",
    "Startup/MSME User",
    "National Research Lab User",
    "Grassroot innovator",
    "Academic User",
)
# Create Booking ``usage_type`` / ``user_type`` ([SPEC] enum).
BOOKING_USER_TYPES = (
    "External Academic User",
    "Internal Academic User",
    "International User",
    "Industry User",
    "Start-ups Company",
    "National Research Lab User",
    "Self (Project Investigator(s))",
    "IAP Member",
    "Academic (Internal but different department/lab)",
)
SERVICE_TYPES = (
    "Characterization",
    "Fabrication",
    "Packaging",
    "Data Monitoring",
    "Analytical Measurement",
    "Drug related Equipment",
)
# [SOP] validation column for equipment_uom: "Unit of measurement for equipment Hour/Minute".
EQUIPMENT_UOMS = ("Hour", "Minute")
REMOTE_STATUS_CODES = ("Pending", "Slot Allocated", "Executed", "Cancelled", "Rejected")

# --- Create User (SOP / PDF) ----------------------------------------------------------------------------------------
# [STAGING] Success: {"status": "SUCCESS", "user_id": <int>} (registration then awaits I-STEM approval). An existing
# email or mobile: {"result": "ERROR", "message": "Mobile number already exists" | "EmailId already exists",
# "data": [{"user_id": "<id>", ...}]}. user_contactno must be exactly 10 digits. A deactivated user's email and
# mobile can be registered again. Edit User refuses the user's own unchanged email/mobile as duplicates (I-STEM bug).
CREATE_USER_FIELDS = (
    "user_first_name", "user_last_name", "user_email", "user_contactno", "user_organisation", "user_type",
    "user_gender", "user_salutation", "user_address", "user_city", "user_district", "user_state", "user_country",
    "user_pin", "billing_user_name", "billing_address", "institute_id", "user_status",
)
USER_GENDERS = ("Male", "Female", "Other")

# --- Create Booking -------------------------------------------------------------------------------------------------
# [SPEC] requestBody.required, identical to the [TEAM] list. Every other field is marked "Optional" in the spec
# (this resolves the older [SOP]/PDF marking of remarks, booking_by, req_unit, ... as required) and is not sent.
# [SPEC] Idempotent: an identical booking (same equipment, user and requested slot) that is not Cancelled/Rejected is
# returned instead of a duplicate.
# [STAGING] booking_remark is required in practice: without it Create Booking answers "Sucess" with new_service_id 0
# and creates nothing.
CREATE_BOOKING_MANDATORY_FIELDS = (
    "equipment_id", "public_user_id", "preferred_timings_from", "preferred_timings_to", "service_type",
    "billing_user_name", "billing_address", "billing_state", "request_date", "booking_title", "total_est_amt",
    "eq_rate", "usage_type", "booking_no_sample", "equipment_uom", "analysis_type", "working_hrs", "booking_gstn",
    "user_type", "booking_remark",
)
# [STAGING] A booking is only confirmed completely (confirmation amount recorded, listed with its confirmation_id)
# when both Create and Confirm carry the staging spec's full field set; the 19 [SPEC] fields alone leave the
# confirmation without an amount and out of every listing. These are always sent, with neutral values where the
# portal has nothing to say. "consumables_provided by" (with a space) is the staging spec's own field name.
CREATE_BOOKING_DESCRIPTIVE_FIELDS = (
    "remarks", "booking_by", "req_unit", "bk_natsamp", "bk_tecinf", "bk_speinf", "background_of_work", "gstin",
    "addl_amt", "exclusive_use", "consumables_provided by", "consumables_provided_by", "specific_information",
    "sample_rows", "sample_name",
)
NOT_APPLICABLE = "NA"

# I-STEM reserves one-hour slots on a 24-hour grid starting at 00:00 IST; IIC slot lengths vary by instrument, so the
# hourly slots covering a booking are sent (mapping.istem_hour_window).
ISTEM_SLOT_HOURS = 1
# [ASSUMED] Words in a Confirm Booking refusal meaning the hourly slot is already taken; I-STEM has not documented the
# message yet (asked in the email to the I-STEM team). Such a refusal moves the booking to the next free hour.
SLOT_OCCUPIED_MARKERS = ("occupied", "already booked", "already allocated", "not available", "clash", "overlap")
# Booking calls that carry the silent-mode flag when ISTEM_SILENT_MODE_FIELD is set (requested from I-STEM).
SILENT_OPERATIONS = frozenset({"create_booking", "confirm_booking", "fbr_execute", "cancel_booking"})


def slot_occupied(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in SLOT_OCCUPIED_MARKERS)
CONSUMABLES_BY_FACILITY = "Institution Facility"

# --- Confirm Booking ------------------------------------------------------------------------------------------------
# [SPEC] requestBody.required. DISCREPANCY: the [SOP] sample re-sent every booking field plus booking_status=true; the
# spec accepts only these plus optional booking_status (default 1), bk_dur, bk_rate, bk_tot_amt, bk_rem, faci_id.
# Only the required fields are sent. Response: top-level confirmation_id; result.request_id (result is {} when the
# booking was already confirmed - idempotent).
CONFIRM_MANDATORY_FIELDS = (
    "request_id", "approved_by", "approved_on", "allocated_timings_from", "allocated_timings_to",
)
# [STAGING] Confirm also re-sends the Create Booking payload and booking_status "Slot Allocated" (see above). The
# response carries no confirmation_id on staging; it is read from Get All Bookings ("Slot Allocated").
# Repeating Confirm answers success again and does not change the first confirmation.
CONFIRM_BOOKING_STATUS = "Slot Allocated"

# --- FBR Execute ----------------------------------------------------------------------------------------------------
# [SPEC] requestBody.required; updated_by optional (defaults to "API_USER"). Response: status SUCCESS, payment_id,
# request_id, confirmation_id (also when already executed - idempotent). A confirmation_id that does not belong to the
# request_id is refused.
# [STAGING] Verified: success, and a repeat returns the same payment_id (an int the first time, a string after). The
# request is keyed on confirmation_id: sent with another booking's request_id it executes and echoes the
# confirmation's own request_id, which the client refuses as a mismatch.
FBR_EXECUTE_FIELDS = ("request_id", "completion_date", "allocation_date", "final_amount", "confirmation_id")
FBR_EXECUTE_OPTIONAL_FIELDS = ("updated_by",)

# --- Remote idempotency ([SPEC] + [TEAM], October 2026) -------------------------------------------------------------
# Repeating these calls with the same parameters returns the existing result instead of a duplicate, so an outcome
# that is uncertain (timeout, lost connection, 5xx, malformed answer) may be retried with exactly the same payload.
IDEMPOTENT_OPERATIONS = frozenset({"CREATE_BOOKING", "CONFIRM_BOOKING", "EXECUTE_FBR"})

"""Everything a signed-in user can do on the portal, as one catalogue shared by search and the IITR Assistant.

Each action names the roles that may use it, where it happens (a page) and, when it is about one instrument, which
equipment to choose from: instruments the user looks after (``manage``) or may book (``book``). A few equipment
switches (Slot Exchange, Split Booking, user ratings) are changed directly, through the same admin serializer as
the equipment form, so the same rules apply (Slot Exchange: Main Admin only).

Matching is plain keywords and phrases, no AI: "enable slot exchange for fesem", "block slots", "where do I recharge".
Pages still enforce their own access; the catalogue only decides what to offer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

ADMIN, DEPT_ADMIN, OIC, LAB, ACCOUNTS = "admin", "dept_admin", "oic", "lab", "accounts"
USER, STUDENT, FACULTY, EXTERNAL, ANY = "user", "student", "faculty", "external", "any"
STAFF = frozenset({ADMIN, DEPT_ADMIN, OIC, LAB})
MANAGERS = frozenset({ADMIN, DEPT_ADMIN, OIC})
BOOKERS = frozenset({USER, ADMIN, DEPT_ADMIN, OIC})


@dataclass(frozen=True)
class PortalAction:
    key: str
    title: str
    hint: str
    keywords: tuple[str, ...]
    roles: frozenset[str]
    path: str = ""
    equipment: str = ""  # "" | "manage" | "book"
    switch: str = ""  # Equipment boolean field changed directly
    group: str = "Pages"

    def target(self, equipment_id: int | None = None) -> str:
        return self.path.replace("{id}", str(equipment_id or ""))

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "title": self.title, "hint": self.hint, "path": self.path, "group": self.group,
            "needs_equipment": bool(self.equipment), "switch": bool(self.switch),
        }


def _a(key, title, hint, keywords, roles, path="", *, equipment="", switch="", group="Pages") -> PortalAction:
    roles = frozenset({roles} if isinstance(roles, str) else roles)
    return PortalAction(key, title, hint, tuple(k.lower() for k in keywords), roles, path, equipment, switch, group)


_E = "Equipment"
_W = "Wallet and payments"
_B = "Bookings"
_S = "Settings"
_P = "People and access"
_R = "Research and results"
_T = "Training"
_L = "Lab operations"

ACTIONS: tuple[PortalAction, ...] = (
    # --- Equipment switches, per instrument ----------------------------------------------------------------------
    _a("slot_exchange", "Turn Slot Exchange on or off", "Let internal users swap booked slots on an instrument",
       ("slot exchange", "exchange slot", "exchange slots", "swap slot", "swap slots", "slot swap"),
       ADMIN, equipment="manage", switch="slot_exchange_enabled", group=_E),
    _a("split_booking", "Turn Split Booking on or off", "Allow non-consecutive slots when continuous ones are not free",
       ("split booking", "split slots", "non consecutive slots", "non-consecutive"),
       ADMIN, equipment="manage", switch="split_booking_enabled", group=_E),
    _a("user_rating", "Turn user ratings on or off", "Let users rate completed bookings of an instrument",
       ("user rating", "user ratings", "rating", "ratings", "star rating", "feedback rating"),
       ADMIN, equipment="manage", switch="user_rating_enabled", group=_E),
    # --- Equipment pages, per instrument -------------------------------------------------------------------------
    _a("change_slot_status", "Change slot status", "Block, open or reserve slots of an instrument",
       ("change slot status", "slot status", "block slot", "block slots", "unblock", "open slots", "close slots",
        "reserve slots", "holiday slots", "blocked slots", "maintenance slots"),
       {ADMIN, OIC}, "/change-slot-status?equipment_id={id}", equipment="manage", group=_E),
    _a("training_authorisations", "Training authorisations", "Who is authorised to use an instrument",
       ("training authorisation", "authorisation", "authorization", "authorised users", "authorized users",
        "grant access", "authorise user"),
       MANAGERS, "/training/authorisations?equipment={id}", equipment="manage", group=_T),
    _a("flash_message", "Equipment flash message", "Show a notice to users of an instrument",
       ("flash message", "flash messages", "equipment notice", "equipment announcement", "banner"),
       MANAGERS, "/equipment-flash-messages?equipment={id}", equipment="manage", group=_E),
    _a("equipment_maintenance", "Equipment maintenance records", "Service and maintenance history of an instrument",
       ("maintenance record", "maintenance history", "service record", "amc", "repair"),
       ADMIN, "/procurement/maintenance?equipment={id}", equipment="manage", group=_E),
    _a("book_equipment", "Book an instrument", "Start a booking",
       ("book", "booking", "new booking", "reserve", "book slot", "make a booking"),
       BOOKERS, "/book-equipment?equipment_id={id}", equipment="book", group=_B),
    _a("equipment_details", "Equipment details", "Specifications, charges, contacts and manuals",
       ("equipment details", "specification", "specifications", "equipment page", "about equipment", "manual"),
       ANY, "/equipment/{id}", equipment="book", group=_E),
    _a("equipment_references", "Manufacturer references", "Product pages, datasheets and manuals the IITR Assistant cites",
       ("manufacturer reference", "manufacturer references", "datasheet", "datasheets", "product page",
        "manufacturer documentation", "technical documentation", "brochure"),
       STAFF, "/equipment-references?equipment={id}", equipment="manage", group=_E),
    # --- Bookings ------------------------------------------------------------------------------------------------
    _a("my_bookings", "My bookings", "See, cancel or reschedule your bookings",
       ("my bookings", "my booking", "cancel booking", "reschedule", "booking status", "view booking"),
       BOOKERS, "/my-bookings", group=_B),
    _a("slot_exchanges", "Slot exchanges", "Offer or accept a slot swap with another user",
       ("my slot exchange", "slot exchange request", "exchange request", "swap request"),
       {USER, STUDENT, FACULTY}, "/slot-exchanges", group=_B),
    _a("booking_templates", "Booking templates", "Reuse saved sample inputs", ("template", "templates", "saved inputs"),
       BOOKERS, "/booking-templates", group=_B),
    _a("urgent_request", "Urgent booking request", "Ask for a priority slot", ("urgent", "urgent request", "priority slot"),
       STUDENT, "/my-urgent-requests", group=_B),
    _a("urgent_requests_wallet", "Urgent requests from my students", "Approve students' urgent requests",
       ("urgent request", "urgent requests", "approve urgent"), FACULTY, "/urgent-requests-wallet", group=_B),
    _a("booking_attempts", "My booking attempts", "Why a booking did not go through",
       ("booking attempt", "booking failed", "failed booking", "attempts"), BOOKERS, "/my-booking-attempts", group=_B),
    _a("availability", "Equipment availability", "Free slots across instruments",
       ("availability", "free slots", "available slots", "when is free"), ANY, "/availability", group=_B),
    _a("booking_calendar", "Booking calendar", "Calendar of bookings", ("calendar", "booking calendar", "schedule"),
       ANY, "/booking-calendar", group=_B),
    _a("find_instrument", "Instrument finder", "Describe your sample and goal to find an instrument",
       ("which instrument", "which equipment", "find instrument", "instrument finder", "recommend instrument"),
       ANY, "/find-instrument", group=_B),
    _a("courier", "Sample dispatch and courier tracking", "Send samples and track the courier",
       ("courier", "dispatch", "send sample", "ship sample", "tracking"), {USER, EXTERNAL, ADMIN, OIC, LAB},
       "/courier-tracking", group=_B),
    _a("remote_analysis", "Remote analysis", "Analyse data remotely on instrument software",
       ("remote analysis", "remote desktop", "rdp", "dsa", "analysis software"), ANY, "/remote-analysis", group=_R),
    _a("special_requests", "Special booking requests", "Requests outside the normal booking flow",
       ("special request", "special booking"), EXTERNAL, "/external-special-requests", group=_B),
    _a("propose_equipment", "Propose new equipment", "Suggest an instrument for the centre",
       ("propose equipment", "suggest equipment", "new instrument request"), ANY, "/propose-equipment", group=_E),
    # --- Results and research ------------------------------------------------------------------------------------
    _a("my_results", "My results", "Download results of completed bookings",
       ("results", "my results", "download results", "data files", "report files"), BOOKERS, "/my-results", group=_R),
    _a("my_research", "My Research", "Projects, research group and shared results",
       ("my research", "research group", "project", "projects", "workspace"), {USER, STUDENT, FACULTY}, "/my-research",
       group=_R),
    _a("shared_data", "Shared with me", "Results others shared with you", ("shared with me", "shared data", "shared results"),
       BOOKERS, "/shared-data", group=_R),
    _a("my_publications", "My publications", "Add publications that acknowledge IIC",
       ("publication", "publications", "paper", "acknowledge", "acknowledgement", "add publication"),
       {STUDENT, FACULTY}, "/my-publications", group=_R),
    _a("publications", "Publications", "Publications acknowledging IIC", ("all publications", "publication list"),
       ANY, "/publications", group=_R),
    _a("publication_claims", "Publication claims", "Approve publications users submitted",
       ("publication claim", "publication claims", "approve publication", "verify publication"),
       {ADMIN, OIC}, "/publication-claims", group=_R),
    _a("research_recognition", "Researcher recognition", "Hall of Frames and Researcher of the Month",
       ("hall of frames", "hall of fame", "researcher of the month", "recognition"), ADMIN, "/admin/research-recognition",
       group=_R),
    # --- Wallet and payments -------------------------------------------------------------------------------------
    _a("wallet", "Wallet", "Balance, transactions and recharge", ("wallet", "balance", "transactions", "money"),
       BOOKERS, "/wallet", group=_W),
    _a("recharge", "Recharge wallet", "Add money to your wallet",
       ("recharge", "top up", "topup", "add money", "add funds", "pay"), BOOKERS, "/wallet/direct-recharge", group=_W),
    _a("credit_facility", "Credit facility", "Request booking on credit", ("credit facility", "credit", "book on credit"),
       {FACULTY, ADMIN}, "/wallet/credit-facility", group=_W),
    _a("proforma", "Proforma invoice", "Quotation for analysis charges", ("proforma", "quotation", "quote", "estimate invoice"),
       BOOKERS, "/proforma-invoice", group=_W),
    _a("analysis_charges", "Analysis charges", "Rates for every instrument", ("charges", "rates", "price", "tariff", "fees", "cost"),
       ANY, "/analysis-charges", group=_W),
    _a("wallet_recharge_requests", "Wallet recharge requests", "Approve or reject recharges",
       ("recharge request", "recharge requests", "approve recharge", "reject recharge"), {ADMIN, DEPT_ADMIN},
       "/admin-settings/wallet-recharge-requests", group=_W),
    _a("sric_recharges", "SRIC wallet recharges", "Recharges from SRIC project funds", ("sric", "project fund", "grant recharge"),
       ADMIN, "/admin-settings/sric-wallet-recharges", group=_W),
    _a("withdrawals", "Wallet withdrawal requests", "Refund wallet balance requests", ("withdrawal", "withdraw", "refund balance"),
       ADMIN, "/admin-settings/wallet-withdrawal-requests", group=_W),
    _a("wallet_ledger", "Wallet ledger", "Every wallet's credits and debits", ("ledger", "wallet ledger", "statement"),
       ADMIN, "/admin/wallet-ledger", group=_W),
    _a("payment_modes", "Wallet payment modes", "Which recharge methods are allowed", ("payment mode", "payment modes", "razorpay", "online payment"),
       ADMIN, "/admin-settings/wallet-payment-modes", group=_W),
    _a("credit_management", "Wallet credit management", "Approve and manage credit facilities",
       ("credit management", "approve credit", "credit limit"), ADMIN, "/admin/wallet-credit", group=_W),
    _a("cash_book", "Wallet recharge cash book", "Match bank entries to recharges", ("cash book", "bank entries", "bills sric", "reconcile"),
       ADMIN, "/admin-settings/wallet-recharge-parse", group=_W),
    _a("invoice_requests", "Invoice requests", "Raise and send invoices", ("invoice", "invoices", "invoice request", "tax invoice"),
       {ACCOUNTS, ADMIN}, "/accounts/invoice-requests", group=_W),
    _a("invoice_dispatch", "Invoice dispatch", "Send prepared invoices", ("dispatch invoice", "send invoice"),
       {ACCOUNTS, ADMIN}, "/accounts/invoice-dispatch", group=_W),
    _a("refunds", "Refunds", "External booking refunds", ("refund", "refunds"), {ACCOUNTS, ADMIN}, "/accounts/refunds", group=_W),
    # --- Lab operations ------------------------------------------------------------------------------------------
    _a("booking_management", "Booking management", "Approve, complete and manage bookings",
       ("booking management", "manage bookings", "complete booking", "approve booking", "mark complete", "upload results"),
       {ADMIN, DEPT_ADMIN, OIC, LAB}, "/booking-management", group=_L),
    _a("urgent_requests", "Urgent requests", "Allocate priority slots", ("urgent", "urgent requests", "allocate urgent"),
       {ADMIN, DEPT_ADMIN, OIC}, "/urgent-requests", group=_L),
    _a("repeat_samples", "Repeat sample requests", "Approve repeat measurements", ("repeat sample", "repeat samples", "rerun"),
       {ADMIN, DEPT_ADMIN, OIC}, "/repeat-sample-requests", group=_L),
    _a("waitlist", "Equipment waitlist", "Users waiting for slots", ("waitlist", "waiting list", "queue"),
       {ADMIN, DEPT_ADMIN, OIC}, "/equipment-waitlist", group=_L),
    _a("disruptions", "Disruptions", "Breakdowns, maintenance and affected bookings",
       ("disruption", "disruptions", "breakdown", "outage", "under maintenance", "equipment down"),
       {ADMIN, DEPT_ADMIN, OIC}, "/disruptions", group=_L),
    _a("attempt_log", "Booking attempt log", "Failed booking attempts by users", ("attempt log", "booking attempt log", "failed attempts"),
       {ADMIN, DEPT_ADMIN, OIC}, "/booking-attempt-logs", group=_L),
    _a("oic_substitute", "OIC substitute", "Hand over your instruments while away", ("substitute", "oic substitute", "temporary oic", "handover"),
       {ADMIN, OIC}, "/oic-substitute", group=_L),
    _a("leave", "Leave management", "Leave and unavailability", ("leave", "unavailability", "absent", "on leave"),
       {ADMIN, OIC}, "/oic-leave-management", group=_L),
    _a("intimate_unavailability", "Intimate unavailability", "Tell users you are unavailable",
       ("leave", "unavailability", "unavailable", "absent"), LAB, "/leave-management", group=_L),
    _a("team_calendar", "Team calendar", "Who is available when", ("team calendar", "staff calendar"), {ADMIN, DEPT_ADMIN},
       "/team-calendar", group=_L),
    _a("accessories", "Accessories", "Optional accessories and their charges", ("accessory", "accessories"), {ADMIN, OIC},
       "/oic/accessories", group=_E),
    _a("fabrication_materials", "Fabrication materials", "3D printing and fabrication materials",
       ("fabrication", "3d print", "print material", "filament"), {ADMIN, OIC, DEPT_ADMIN}, "/oic/print-materials", group=_E),
    _a("equipment_configuration", "Equipment configuration", "Booking rules, inputs and settings of your instruments",
       ("equipment configuration", "equipment settings", "configure equipment", "booking rules", "inputs", "sample inputs"),
       {ADMIN, OIC}, "/oic/equipment-settings", group=_E),
    _a("multi_mode", "Multi-mode equipment", "Instruments with several modes", ("multi mode", "multi-mode", "modes"),
       {ADMIN, OIC}, "/multi-mode-equipment", group=_E),
    _a("quota", "Quota configuration", "Booking quotas per user type", ("quota", "quotas", "booking limit", "limit"),
       {ADMIN, OIC}, "/oic/quota-configurations", group=_E),
    _a("notice_board_requests", "Notice board requests", "Approve notices for the home page", ("notice board", "notice", "notices"),
       {ADMIN, OIC}, "/notice-board-requests", group=_S),
    _a("reports", "Reports and statistics", "Usage, revenue and exports", ("report", "reports", "statistics", "revenue", "usage", "export"),
       {ADMIN, DEPT_ADMIN, OIC, ACCOUNTS, USER}, "/reports", group=_R),
    _a("my_duty", "My operator duty", "Your duty roster", ("duty", "roster", "my duty"), {USER, STUDENT, LAB}, "/my-duty", group=_T),
    # --- Training ------------------------------------------------------------------------------------------------
    _a("my_trainings", "My trainings", "Training and certification", ("training", "trainings", "certification", "certificate"),
       BOOKERS, "/my-trainings", group=_T),
    _a("my_authorisations", "My authorisations", "Instruments you are authorised to use",
       ("my authorisation", "my authorization", "am i authorised", "authorised to use"), BOOKERS, "/my-authorisations", group=_T),
    _a("training_events", "Training and demos", "Sign up for training sessions and demos", ("demo", "demos", "training session", "workshop"),
       ANY, "/training/nominations", group=_T),
    _a("training_workspace", "Training workspace", "Run trainings for your instruments", ("training workspace", "conduct training"),
       {ADMIN, OIC}, "/training/oic", group=_T),
    _a("ta_nominations", "TA nominations", "Teaching assistant nominations", ("ta nomination", "teaching assistant", "ta call"),
       {ADMIN, OIC, STUDENT}, "/ta-nomination-call", group=_T),
    _a("rewards", "TA reward points", "Your reward points", ("reward", "rewards", "points"), STUDENT, "/rewards", group=_T),
    # --- People and access ---------------------------------------------------------------------------------------
    _a("user_management", "User management", "Users, roles and permissions", ("user management", "manage users", "roles", "change role", "deactivate user"),
       ADMIN, "/user-management", group=_P),
    _a("registration_requests", "Registration requests", "Approve new accounts", ("registration", "new users", "approve user", "signup"),
       ADMIN, "/admin/registration-requests", group=_P),
    _a("external_users", "External user management", "External users and organisations", ("external user", "external users", "organisation verification"),
       ADMIN, "/manage/external-user-management", group=_P),
    _a("student_management", "Student management", "Your students and their wallet access", ("my students", "student management", "link student", "join request"),
       FACULTY, "/student-management", group=_P),
    _a("department_administration", "Department administration", "Staff modules and permission caps",
       ("department administration", "rbac", "permissions", "staff modules"), {ADMIN, DEPT_ADMIN}, "/admin/department-administration", group=_P),
    _a("users_overview", "Users overview", "Everyone, with ID cards", ("users overview", "all users", "user list", "id card"),
       {ADMIN, DEPT_ADMIN}, "/admin/insights/users", group=_P),
    _a("user_groups", "User groups and email", "Email groups of booking users", ("user groups", "email users", "bulk email", "mailing list"),
       ADMIN, "/admin-settings/user-groups", group=_P),
    _a("admin_panel_access", "Admin panel access", "Admin modules per user type and department", ("admin panel access", "panel access"),
       ADMIN, "/admin-settings/admin-panel-access", group=_P),
    # --- Settings ------------------------------------------------------------------------------------------------
    _a("admin_settings", "Admin settings", "All institute-wide settings", ("admin settings", "settings", "configuration"),
       {ADMIN, DEPT_ADMIN}, "/admin-settings", group=_S),
    _a("equipment_admin", "Equipment administration", "Add or edit instruments", ("add equipment", "edit equipment", "new equipment", "equipment list"),
       {ADMIN, DEPT_ADMIN}, "/admin/section/equipment", group=_S),
    _a("departments", "Departments", "Department list and visibility", ("department", "departments"), ADMIN, "/admin/section/departments", group=_S),
    _a("department_modules", "Department modules", "Switch modules per department", ("department modules", "module switch", "enable module"),
       ADMIN, "/admin/department-modules", group=_S),
    _a("session_timeout", "Session and auto-logout", "Inactivity timeout and peak window", ("session", "auto logout", "timeout", "peak window", "inactivity"),
       ADMIN, "/admin-settings/auth", group=_S),
    _a("email_templates", "Communication and email templates", "Email templates and logs", ("email template", "email templates", "communication", "email log"),
       ADMIN, "/admin-settings/communication", group=_S),
    _a("booking_charge_settings", "Booking charge settings", "How charges are calculated", ("charge settings", "booking charges", "gst"),
       ADMIN, "/admin-settings/equipment/booking-charge-settings", group=_S),
    _a("booking_buffer", "Booking buffer", "Gaps between bookings", ("buffer", "booking buffer", "gap between bookings"),
       ADMIN, "/admin-settings/equipment/booking-buffer-config", group=_S),
    _a("semesters", "Semesters", "Semester dates", ("semester", "semesters", "academic calendar"), ADMIN, "/admin-settings/equipment/semesters", group=_S),
    _a("mode_schedules", "Mode schedules", "When each mode of an instrument runs", ("mode schedule", "mode schedules"),
       ADMIN, "/admin-settings/equipment/mode-schedules", group=_S),
    _a("search_synonyms", "Search synonyms", "Words search treats as the same", ("synonym", "synonyms", "search words"),
       ADMIN, "/admin-settings/equipment/search-synonyms", group=_S),
    _a("hero_slides", "Home page slides and content", "Home page content", ("hero", "slides", "home page", "content management"),
       ADMIN, "/content-management", group=_S),
    _a("calendar_colors", "Calendar colours", "Colours of booking states", ("calendar colour", "calendar color", "colours", "colors"),
       ADMIN, "/calendar-colors", group=_S),
    _a("automation", "AI and automation", "Instrument finder, best times and automatic alerts", ("automation", "ai settings", "no show", "alerts"),
       ADMIN, "/admin-settings/automation", group=_S),
    _a("assistant_knowledge", "IITR Assistant knowledge", "SOPs and FAQs the assistant answers from",
       ("knowledge", "sop", "faq", "assistant knowledge"), ADMIN, "/admin-settings/knowledge", group=_S),
    _a("support_admin", "Support tickets (helpdesk)", "Reassign and resolve tickets", ("helpdesk", "support queue", "all tickets"),
       ADMIN, "/admin-settings/support", group=_S),
    _a("experience_ratings", "Experience ratings", "Ratings and comments", ("experience rating", "ratings report", "feedback report"),
       ADMIN, "/admin-settings/feedback", group=_S),
    _a("procurement", "Procurement and assets", "Purchases, assets and maintenance", ("procurement", "purchase", "asset", "assets", "vendor"),
       ADMIN, "/procurement", group=_S),
    _a("inventory", "Inventory management", "Consumables and stock", ("inventory", "stock", "consumables"), ADMIN, "/inventory-management", group=_S),
    _a("lifecycle", "Equipment lifecycle and expenses", "Costs over an instrument's life", ("lifecycle", "expenses", "depreciation"),
       ADMIN, "/equipment-lifecycle", group=_S),
    _a("cancellations", "Cancellations dashboard", "Cancellations and their reasons", ("cancellation report", "cancellations"),
       ADMIN, "/admin/insights/cancellations", group=_R),
    _a("equipment_overview", "Equipment overview", "Utilisation of every instrument", ("utilisation", "utilization", "equipment overview"),
       {ADMIN, DEPT_ADMIN}, "/admin/insights/equipment", group=_R),
    # --- Everyone ------------------------------------------------------------------------------------------------
    _a("notifications", "Notification settings", "Choose which emails and alerts you get",
       ("notification", "notifications", "email alerts", "unsubscribe", "mute"), ANY, "/notifications?tab=settings", group=_S),
    _a("profile", "Profile", "Your details, phone and supervisor", ("profile", "phone number", "supervisor", "my details", "photo"),
       ANY, "/profile", group=_P),
    _a("tickets", "Support tickets", "Ask for help or report a problem", ("help", "support", "ticket", "complaint", "problem", "issue"),
       ANY, "/tickets", group=_S),
    _a("user_guide", "User guide", "How to use the portal", ("user guide", "guide", "how to use", "tutorial"), ANY, "/user-guide", group=_S),
)
BY_KEY = {a.key: a for a in ACTIONS}

_STOP = frozenset(
    "a an the to for of on in at my me i we our is are be can could would should please how do does where what which "
    "with and or want need like this that it from".split()
)
_ON = re.compile(r"\b(enable|enabled|turn on|switch on|activate|allow|start|open up)\b")
_OFF = re.compile(r"\b(disable|disabled|turn off|switch off|deactivate|stop|block)\b")
_WORD = re.compile(r"[a-z0-9]+")


def roles_of(user) -> frozenset[str]:
    from iic_booking.users.models.user_type import UserType

    if not getattr(user, "is_authenticated", False):
        return frozenset()
    t = str(getattr(user, "user_type", "") or "").lower()
    out = {ANY}
    if getattr(user, "is_superuser", False) or t == UserType.ADMIN:
        out.add(ADMIN)
    out |= {
        UserType.DEPT_ADMIN: {DEPT_ADMIN}, UserType.MANAGER: {OIC}, UserType.OPERATOR: {LAB}, UserType.FINANCE: {ACCOUNTS},
        UserType.STUDENT: {USER, STUDENT}, UserType.INDIVIDUAL_STUDENT: {USER, STUDENT}, UserType.FACULTY: {USER, FACULTY},
    }.get(t, set())
    if UserType.is_end_user_booking_type(t) and t not in (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT, UserType.FACULTY):
        out |= {USER, EXTERNAL}
    return frozenset(out)


def available(user) -> list[PortalAction]:
    roles = roles_of(user)
    return [a for a in ACTIONS if a.roles & roles]


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def score(action: PortalAction, text: str) -> int:
    """Phrase hits count most (longer phrases more); words of the title count a little."""
    lower = " " + " ".join(_WORD.findall((text or "").lower())) + " "
    words = set(_words(text))
    s = 0
    for k in action.keywords:
        if f" {k} " in lower:
            s = max(s, 10 + 4 * len(k.split()))
    title_words = set(_words(action.title))
    overlap = len(words & title_words)
    if overlap and title_words:
        s += 3 * overlap if overlap >= min(2, len(title_words)) else overlap
    return s


def match(user, text: str, *, limit: int = 6, min_score: int = 6) -> list[PortalAction]:
    ranked = sorted(((score(a, text), i, a) for i, a in enumerate(available(user))), key=lambda r: (-r[0], r[1]))
    return [a for s, _, a in ranked if s >= min_score][:limit]


# Words too common to say which action someone means ("booking", "settings", ...): never a loose match on their own.
_GENERIC = frozenset({
    "book", "booking", "bookings", "slot", "slots", "equipment", "instrument", "instruments", "settings", "setting",
    "page", "open", "show", "find", "change", "turn", "user", "users", "list", "status", "request", "requests",
})


def loose_match(user, text: str, *, limit: int = 4) -> list[PortalAction]:
    """Actions whose keywords or title contain a distinctive word of a short query ("exchange", "flash", "amc").

    Used only to offer choices when nothing matches strongly; the most words in common rank first."""
    words = {w for w in _words(text) if len(w) >= 3 and w not in _GENERIC}
    if not words:
        return []
    ranked = []
    for i, a in enumerate(available(user)):
        vocab = set(_words(a.title))
        for k in a.keywords:
            vocab |= set(_WORD.findall(k))
        hit = len(words & vocab)
        if hit:
            ranked.append((-hit, i, a))
    return [a for _, _, a in sorted(ranked, key=lambda r: (r[0], r[1]))][:limit]


def desired_state(text: str) -> bool | None:
    lower = (text or "").lower()
    on, off = bool(_ON.search(lower)), bool(_OFF.search(lower))
    return None if on == off else on


# --- Equipment the user may choose for an action ------------------------------------------------------------------

def equipment_scope(user, action: PortalAction):
    """Equipment queryset for the action: ``manage`` = what the user looks after (Main Admin: everything; Department
    Administrator: their department's; OIC / Lab In-charge: their own); ``book`` = what they can see and book."""
    from .global_search import _searchable_equipment_ids
    from .models import Equipment, EquipmentManager, EquipmentOperator
    from .testdata import exclude_test_equipment, is_test_user

    roles = roles_of(user)
    qs = Equipment.objects.all()
    if action.equipment == "book":
        qs = qs.filter(pk__in=_searchable_equipment_ids(user))
    elif ADMIN in roles:
        pass
    elif DEPT_ADMIN in roles:
        qs = qs.filter(internal_department_id=getattr(user, "department_id", None))
    else:
        mine = set(EquipmentManager.objects.filter(manager=user).values_list("equipment_id", flat=True))
        mine |= set(EquipmentOperator.objects.filter(operator=user).values_list("equipment_id", flat=True))
        qs = qs.filter(pk__in=mine)
    if not is_test_user(user):
        qs = exclude_test_equipment(qs)
    return qs.exclude(status="DISPOSED")


def equipment_choices(user, action: PortalAction, query: str = "", *, limit: int = 12) -> list[dict[str, Any]]:
    qs = equipment_scope(user, action)
    words = _words(query)
    if words:
        from django.db.models import Q

        for w in words:
            qs = qs.filter(Q(name__icontains=w) | Q(code__icontains=w))
    rows = list(qs.order_by("name").values("equipment_id", "name", "code", *([action.switch] if action.switch else []))[:limit])
    return [
        {"id": r["equipment_id"], "name": r["name"], "code": r["code"],
         **({"on": bool(r[action.switch])} if action.switch else {})}
        for r in rows
    ]


def equipment_named_in(user, action: PortalAction, text: str):
    """The one instrument in the action's scope that ``text`` names (by code, or every word of a name), else None."""
    leftover = set(_words(text))
    for k in action.keywords + (action.title.lower(),):
        leftover -= set(k.split())
    leftover -= {"enable", "disable", "turn", "switch", "off", "activate", "deactivate", "allow", "change", "set", "open",
                 "equipment", "instrument", "machine"}
    if not leftover:
        return None
    candidates = list(equipment_scope(user, action).values("equipment_id", "name", "code"))
    hits = [c for c in candidates if (c["code"] or "").lower() in leftover]
    if not hits:
        hits = [c for c in candidates if leftover <= set(_words(c["name"])) | {(c["code"] or "").lower()}]
    if not hits:
        hits = [c for c in candidates if any(w in _words(c["name"]) for w in leftover if len(w) >= 3)]
    return hits[0] if len(hits) == 1 else None


class ActionRefused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


def run_switch(request, action: PortalAction, equipment_id: int, on: bool) -> dict[str, Any]:
    """Change an equipment switch through the equipment form's serializer (same validation and rules)."""
    from .serializers import EquipmentAdminWriteSerializer

    if not action.switch:
        raise ActionRefused("This action has no switch.")
    if not (action.roles & roles_of(request.user)):
        raise ActionRefused("You cannot change this setting.", 403)
    equipment = equipment_scope(request.user, action).filter(pk=equipment_id).first()
    if equipment is None:
        raise ActionRefused("Equipment not found.", 404)
    serializer = EquipmentAdminWriteSerializer(equipment, data={action.switch: bool(on)}, partial=True,
                                               context={"request": request})
    if not serializer.is_valid():
        errors = serializer.errors
        first = next(iter(errors.values()), ["Not saved."])
        raise ActionRefused(str(first[0] if isinstance(first, list) else first))
    serializer.save()
    equipment.refresh_from_db()
    return {"id": equipment.pk, "name": equipment.name, "code": equipment.code, "on": bool(getattr(equipment, action.switch))}


def search_hits(user, text: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Actions for the header search (with the equipment named in the query, when there is exactly one)."""
    out = []
    for a in match(user, text, limit=limit):
        row = a.as_dict()
        if a.equipment:
            named = equipment_named_in(user, a, text)
            if named:
                row["equipment"] = {"id": named["equipment_id"], "name": named["name"], "code": named["code"]}
        out.append(row)
    return out


def keys(actions: Iterable[PortalAction]) -> list[str]:
    return [a.key for a in actions]


# --- API ------------------------------------------------------------------------------------------------------------

from rest_framework import status as http  # noqa: E402
from rest_framework.decorators import api_view, permission_classes  # noqa: E402
from rest_framework.permissions import IsAuthenticated  # noqa: E402
from rest_framework.response import Response  # noqa: E402


def _allowed(user, key: str) -> PortalAction | None:
    action = BY_KEY.get(key)
    return action if action is not None and action.roles & roles_of(user) else None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_actions_list(request):
    """GET ?q= : actions matching the words; without ``q``, every action this user can use (grouped in the UI)."""
    q = " ".join((request.query_params.get("q") or "").split())[:80]
    if q:
        return Response({"actions": search_hits(request.user, q, limit=10)})
    return Response({"actions": [a.as_dict() for a in available(request.user)]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def portal_action_equipment(request, key: str):
    """GET ?q= : the instruments this user may choose for the action (with each switch's current state)."""
    action = _allowed(request.user, key)
    if action is None or not action.equipment:
        return Response({"error": "Not found."}, status=http.HTTP_404_NOT_FOUND)
    q = " ".join((request.query_params.get("q") or "").split())[:80]
    return Response({"action": action.as_dict(), "equipment": equipment_choices(request.user, action, q, limit=25)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def portal_action_run(request, key: str):
    """POST {equipment_id, on} : change an equipment switch (Slot Exchange, Split Booking, user ratings)."""
    action = _allowed(request.user, key)
    if action is None or not action.switch:
        return Response({"error": "Not found."}, status=http.HTTP_404_NOT_FOUND)
    try:
        equipment_id = int(request.data.get("equipment_id"))
    except (TypeError, ValueError):
        return Response({"error": "Choose an instrument."}, status=http.HTTP_400_BAD_REQUEST)
    on = request.data.get("on") in (True, 1, "1", "true", "on")
    try:
        return Response(run_switch(request, action, equipment_id, on))
    except ActionRefused as exc:
        return Response({"error": exc.message}, status=exc.status)

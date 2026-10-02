"""Seed baseline IIC knowledge articles for Research Copilot (Phase AI.2)."""

from __future__ import annotations

from iic_booking.research_copilot.models import DocumentCategory, KnowledgeDocument, SecurityLevel
from iic_booking.research_copilot.services.ingestion import upsert_document

SEED_ARTICLES: list[dict] = [
    {
        "title": "How to Book Equipment",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["booking", "slots", "faq", "server clock", "important instruction"],
        "external_url": "/equipments",
        "content_text": """
To book equipment on the IIC Equipment Booking Portal:
1. Sign in and open Browse and Book Equipment (or Equipments).
2. Select the instrument matching your measurement need and open it.
3. Review specifications, Calculate charges and the Important instruction. A lab can show a different
   important instruction to each user type, so read the one shown to you.
4. Fill the booking inputs, or pick a saved template under Booking template. If some samples need different
   settings, click Add sample with different parameters below the inputs (offered when the lab allows it).
5. Choose free slots and submit the booking. Your wallet (or your supervisor's wallet) is charged when the
   booking is created; wallet balance, quotas and supervisor spending limits are checked at that moment.
6. Track the booking from View Booking on the dashboard.
Booking windows: next week's slots normally open every Wednesday at 9:00 PM. The booking page shows the
exact window for each account and a live countdown to the next opening, and the server clock in its header shows
portal time (IST) — windows open by that clock. Admins and OICs can book any week.
Peak booking window: during the weekly opening, IIT Roorkee users who click an equipment card go straight to its
booking page, and external users (External, Industry, R&D and other non-IITR accounts) are paused from 8:55 to
9:15 PM on Wednesdays (the default) with a notice beforehand. Admins, OICs and staff are never paused.
On the booking page: the remaining weekly quota is shown before you pick slots; tap a greyed-out slot to see why
it cannot be booked; the Review line above Confirm sums up slots, time and charge. Students without a linked
supervisor wallet see Link supervisor's wallet / Invite your supervisor, and a warning shows when the wallet
cannot cover the charge. If a booking fails, the form stays filled in (only slots someone else took are dropped)
and an unsaved booking is restored as a draft on the same device (Discard clears it).
In the Equipments catalog each card shows a From price and Book now, with a category filter.
You can also book through the Booking Assistant (bottom-right button): choose Book equipment and follow the
steps. Never invent slot availability — always check the equipment page or live slots.
""",
    },
    {
        "title": "Booking Status Meanings",
        "category": DocumentCategory.FAQ,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["status", "waitlisted", "booked", "not utilized", "refunded"],
        "external_url": "/my-bookings",
        "content_text": """
Common booking statuses shown in View Booking:
- Pending: request submitted, awaiting confirmation or the next system step.
- Awaiting payment: payment or wallet debit is still needed before the slot is secured.
- Waitlisted (WL1, WL2, ...): no slot was free; you are in the FCFS waitlist and are not charged until promoted.
- Booked: the slot is confirmed; note the sample submission deadline.
- Awaiting your choice (disruption): maintenance, operator absence or another disruption needs your decision.
- Under Maintenance / Operator Absent / Analysis Not Possible: lab-side holds; watch emails and View Booking.
- Booking Not Utilized: the session did not proceed for user-side reasons (usually no refund).
- Completed: the analysis finished; results may be available in View results.
- Cancelled / Refunded: cancelled under the cancellation policy. Cancelled and refunded bookings keep their
  original start and end dates.
In My Bookings, "What do these statuses mean?" opens the same legend, and each booking shows its cancel /
reschedule deadline, or "Deadline passed - contact the Officer in Charge".
""",
    },
    {
        "title": "Wallet, Recharge and Grants",
        "category": DocumentCategory.POLICY,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["wallet", "recharge", "grant", "sric", "project grant", "cash", "bank transfer", "decline reason"],
        "external_url": "/wallet",
        "content_text": """
Wallet guidance:
- Open Wallet management to see balances (one sub-wallet per department) and transactions. The buttons at the
  top of the Wallet page are Transfer (faculty), Credit Facility and Recharge Wallet.
- Students and project staff book from their supervisor's or PI's wallet: open Wallet, find the faculty under
  Request to Join Wallet and click Send Request; the faculty approves it.
- Supervisor not in the search? The search only lists faculty who have signed in to the portal at least once.
  Click "Invite your supervisor", enter their IIT Roorkee email (optionally their name, department and a short
  message) and click Send invitation. They get an email with a "Sign in to review" button that opens the normal
  portal sign-in. When they sign in, the invitation becomes a normal link request that they approve or reject;
  you get an email and a notification when that happens. Track invitations under "Invitations you sent" with
  Resend (once every 24 hours) and Cancel. Limits: up to 3 pending invitations; each expires after 30 days.
Recharge Wallet (faculty):
1. Choose the Recharge method: Project Grant (sponsored project funds, approved by the SRIC Office) or
   Direct Cash Deposit / Bank Transfer.
2. For Project Grant select the project or Add Project (name, code, funding agency, dates).
3. Choose the department sub-wallet in Credit to and the Amount (minimum Rs 100).
4. Accept the undertaking and enter the OTP sent to your email (valid 10 minutes).
5. Note the Transaction ID. Cash / Bank transfer: deposit or transfer at the SRIC Bill Section and share the
   transaction number with them.
Statuses: Awaiting OTP, Pending, Approved, Approved · awaiting funds, Declined by SRIC, Rejected.
Decline reasons: Project Grant — Wrong Project Code, Insufficient Funds in the Project, Project Already Closed,
Other. Cash / Bank transfer — Mismatch in User Information, Other. The reason is shown on the wallet, and the
emails to the SRIC offices name who approved a recharge.
Declined by SRIC: the amount of a declined Project Grant request is treated as an auto-approved credit
(shown as outstanding) and is adjusted when the funds of the next approved recharge are received. While a
credit is running, an approved Project Grant request is credited only when SRIC confirms the funds
(Approved · awaiting funds).
Students: where student recharge is enabled for the account, Recharge Wallet offers Direct Cash Deposit /
Bank Transfer (same OTP and SRIC Bill Section steps); once approved, the funds go to the supervisor's wallet.
Pay online (card / net banking) appears in the Recharge dialog but stays greyed out with "Awaiting Competent
Authority Approval" until the Main Administrator switches it on.
Uploading a payment receipt is no longer offered as a recharge method — you never need to upload a receipt.
A method shown greyed out with "Awaiting Competent Authority Approval" is switched off by the Main
Administrator. The Booking Assistant will not invent balances: ask it "wallet balance" for your live figure.
""",
    },
    {
        "title": "Wallet Transfer and Credit Facility",
        "category": DocumentCategory.POLICY,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["wallet", "transfer", "credit facility", "credit", "faculty credit"],
        "external_url": "/wallet/credit-facility",
        "content_text": """
Transfer (faculty): Wallet page -> Transfer. Moves balance to another internal user under the same department
grant. Choose From department (grant), Recipient (same grant), Amount and optional remarks, then confirm with an
email OTP. No admin approval is needed. Past transfers are listed under Transfer history.
Credit Facility: Wallet page -> Credit Facility. Eligible faculty, staff and HoDs choose the Department, enter
the Requested Amount and Purpose / Reason, and click Submit Credit Request. The Credit facility rules card shows
the minimum, maximum per request, maximum outstanding, duration and reminders. The Main Administrator approves
each request (Admin Settings -> User Management -> Wallet Credit Management); only one facility can be active at
a time. Requests are tracked under My Credit Facilities.
Faculty Credit Facility: if a department offers it and the faculty member is eligible (date of joining), Avail
credit on that department's sub-wallet gives a one-time negative-balance credit that later recharges recover.
Once closed it cannot be availed again.
Each option can be switched off by the Main Administrator in Wallet payment modes.
""",
    },
    {
        "title": "Booking Templates",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["template", "booking template", "preferred slot", "save as template", "booking templates page"],
        "external_url": "/booking-templates",
        "content_text": """
A booking template saves the booking form for one equipment — inputs, sample sets, booking options and the
research workspace where available — so the same analysis can be booked again quickly. Templates are private,
up to 25 per equipment, only for equipment you are allowed to book, and never book anything by themselves: you
still click Book.
Booking Templates page: on the dashboard open Booking Templates (Open templates). It lists all your templates
grouped by equipment with key inputs, sample sets, booking options and preferred slot; search, filter by
department or equipment, and sort by Group by equipment, Recently updated or Name (A–Z).
Create: on the Booking Templates page click New template, choose a Department, pick the equipment (Find
equipment) and click Continue. You can also start from the booking page (Booking template picker -> Create
template) or Booking templates on the equipment page -> Create template. Fill the form, choose Booking options (Auto-select all required slots,
Add to the waitlist if the booking cannot be completed, Book any available slots, Book even if single slot is
available, and where offered Automatically search and allocate alternate equipment), enter a Template name and
click Save template.
Preferred slot (optional): pick the Day, Start time and Number of slots. Opening the booking page with the
template pre-selects that slot when it is free. Next week's slots open Wednesday at 9:00 PM.
If this slot is already taken when I click Book: Ask me (recommended), Book the next free slot later the same
day, or Book the next free slot on any day I can book. The automatic options need the consent tick: the portal
may book the next free slot of the same length and charge the wallet; wallet balance, spending limits and quotas
are still checked. With Ask me, the page shows Nearest free slots of the same length.
Use: pick a template under Booking template (Choose a template to fill the form). The first template is applied
automatically; choose No template (default form) to start blank.
Manage: on the Booking Templates page use Book now (opens the booking page with the template filled in), Edit
(Update template), and the card menu's Duplicate (the copy's slot-taken choice is reset to Ask me) or Delete
(bookings are not affected). Manage templates on the booking page and Booking templates on the equipment page
offer Edit, Book with this template and delete.
After any booking attempt, Save these parameters as a template saves the inputs; tick Pre-select this slot next
time to remember the slot.
""",
    },
    {
        "title": "Sample Sets and Editing Booking Inputs",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["sample set", "element", "periodic table", "edit inputs", "pay difference", "refund"],
        "external_url": "/my-bookings",
        "content_text": """
Sample sets: the details in Step 1 of the booking form are sample set 1. If some samples need different
settings, click Add sample with different parameters at the bottom of Step 1. Each new set starts with the
equipment's default values (not a copy of set 1); use Copy set 1 values on a set to start from set 1 instead.
Every set has the same fields as set 1, including Select elements (periodic table) and sample tables. Each set
is charged and timed separately and added to the same booking; Step 2 shows the charge of each set. Use
Duplicate, Remove or Collapse on a set as needed. The lab decides per equipment whether sample sets are offered;
when they are not, the button is not shown and a separate booking is needed for samples with other settings.
Bookings made earlier keep their sample sets.
Number fields start at 1 (0 is not accepted) and show "Max N allowed" beside the box at the limit. Limits that
depend on another field (for example No. of Samples up to Number of Slots x 4) apply to every user type and to
each sample set using that set's own values.
Editing inputs after booking: open the booking and choose Edit User Inputs. Values can be changed until the
booking is completed (the Officer In Charge can also edit after completion). Field limits still apply.
If the charge goes up, pay the difference (Pay now) within 1 minute or the edit is cancelled and the previous
values are restored. If the new charge is lower, the difference goes back to the same wallet straight away when
you save the change before the cancellation deadline (the same deadline as for cancelling or rescheduling: 48
hours before the slot unless the lab set another value). The edit form shows this deadline. After the deadline,
the refund needs the Officer In Charge's approval (Confirm refund). Lower charges from edits made by the lab
staff are also confirmed by the Officer In Charge. OICs and admins can use Deduct Money to debit an unpaid
difference from the user's wallet.
""",
    },
    {
        "title": "Booking Assistant — Guided Booking",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["booking assistant", "copilot", "guided booking", "virtual booking id", "confirm booking"],
        "external_url": "/book-equipment",
        "content_text": """
The Booking Assistant is the round button at the bottom-right of the portal. Choose Book equipment for a guided
booking in five steps: Department -> Equipment -> booking inputs (sample sets, element selection and the
equipment's important instruction) -> slot (Earlier / Later to move dates) -> summary.
On the summary, tick "I have read the instructions above" and press Confirm booking. Nothing is booked until
then; typing "confirm" does not book. In-chat booking is rolled out in stages — where it is not enabled the
summary offers Continue on booking page with the details filled in.
After booking the assistant shows the virtual booking ID with View Booking, and Open Analysis Workspace only
when the equipment has Remote Analysis enabled.
At any step use Change slot, Change samples/inputs or Change equipment; details already entered are kept.
Cancel stops without booking.
Free text still works, for example "I need FESEM tomorrow — what are my options?", "What are the TEM charges?"
or "Show my upcoming bookings". Bookings can be referred to by their virtual booking ID.
Day-to-day questions are answered instantly from live portal data: "my recent bookings", "status of
<booking ID>", "wallet balance", "how do I recharge my wallet?", "my transactions", "results of my last
booking", "my waitlist", "my tickets"; staff can ask "today's bookings on my equipment" or "pending approvals".
After a list of bookings each booking has buttons for what the portal allows right now — View details,
Reschedule, Cancel, Edit parameters, Message the lab, View results, Download invoice, Book again — and you can
also type "cancel the second one". Cancel and reschedule always show a summary and need Confirm; edit,
invoice and messages open the booking in My Bookings.
The same checks as the booking page apply: slot length, input limits, wallet balance, quotas and spending limits.
""",
    },
    {
        "title": "Student Management and Spending Limits",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["student management", "spending limit", "identity card", "delink", "ta nomination", "supervisor",
                 "role:faculty"],
        "external_url": "/student-management",
        "content_text": """
Faculty open Student management from the dashboard to see students linked to their wallet (students appear after
the faculty approves their wallet join request).
- If a student invited you by email before you had signed in, their request is waiting under Pending actions and
  in Wallet when you first sign in. Nothing is linked until you click Approve; you can also Reject it.
- Spending limit: set a Weekly limit and/or Monthly limit (Rs) per student and Save; leave empty for no limit.
  Weeks run Monday–Sunday and months are calendar months (IST). Usage This week / This month is shown.
  Counted: bookings the student creates in the period at their current charge; never-charged or fully refunded
  bookings are excluded and an unpaid input-edit difference counts only once paid. Bookings made by the lab
  (OIC or admin) on the student's behalf are not blocked. Students see "Supervisor spending limit" on the
  booking page, and a booking that would exceed the limit is blocked.
- Click a student's name to open the Student identity card.
- Turn off the Linked toggle to delink a student (optional message; Keep linked cancels).
- TA operating nominations: during an open call, click Nominate student; outcomes appear in the Nominations log.
Supervisors receive one booking email per booking made by a linked student, matching the student's email, with a
Booked by row. Supervisors also approve their students' Type B urgent requests under Urgent booking requests.
""",
    },
    {
        "title": "View Booking, Results and Calendar Sync",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["view booking", "my bookings", "filters", "calendar sync", "book again", "results"],
        "external_url": "/my-bookings",
        "content_text": """
The View Booking tile on the dashboard opens your bookings (page title My Bookings). Filters: search, Status,
start and end dates and All equipment; More filters shows the rest; Apply / Clear. Cancelled and refunded
bookings keep their original dates.
Book again opens the booking form with earlier inputs filled in. Sync to calendar adds bookings to Google
Calendar, Outlook or Apple Calendar or gives a private link; subscribing keeps the calendar updated, a one-time
add does not. Published results appear on the booking and under View results.
Staff (Lab Operator, OIC, Department Administrator, Admin) get the staff View Booking page (formerly Booking
Management) with columns S.No., Booking ID, Equipment Name, User Name, Supervisor Name, User Mobile, User Email,
Booking Start Date and Duration — every column is sortable — and a hover box on booked slots.
""",
    },
    {
        "title": "OIC Tools — Urgent Booking, Waitlist, Slot Status and Tickets",
        "category": DocumentCategory.SOP,
        "security_level": SecurityLevel.OPERATOR,
        "tags": ["oic", "urgent booking", "waitlist", "confirm manually", "change slot status", "tickets", "important instruction",
                 "role:operator", "role:admin", "role:dept_admin"],
        "content_text": """
Urgent booking (dashboard, formerly Urgent Requests): Type B requests (urgent with reason, 50% surcharge) from
students arrive after their supervisor approves; the OIC gives final approval and may reschedule, including
weekends. The wallet is charged only after final approval.
Equipment waitlist: opens on the first equipment. Confirm manually -> Confirm waitlisted booking places a
waitlisted booking into any unbooked slot (including weekends, holidays, closed, blocked and maintenance slots);
the charge is debited from the user's wallet. Only OICs and admins can confirm manually.
Change slot status (equipment page menu): double-click a date or drag across dates to open the Week view, which
opens on the current week. The week arrows change week on a single click; double-clicking a date jumps to its
week. Time labels select rows and day headers select columns.
Support tickets: Tickets marked to me lists tickets assigned to the OIC/Lab Operator or raised for their
equipment.
Bookings awaiting completion: dashboard card plus a reminder email every day at 9:00 AM until completed.
Calculate charges works on any catalog equipment (view-only when not assigned); Book for a user and Change slot
status stay on assigned equipment.
Equipment Booking Configuration: Important instruction with rich formatting — Default (all user types) plus
"Add an instruction for a user type". A sample submission lead time of 0 means no sample deadline; if both the
lead time and the sample collect deadline are 0 (walk-in equipment), no sample emails are sent and no automatic
Not Utilized happens.
""",
    },
    {
        "title": "What's New — October 2026",
        "category": DocumentCategory.RELEASE_NOTES,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["what's new", "release notes", "october 2026"],
        "content_text": """
Changes released in September–October 2026:
- Booking templates with an optional preferred weekly slot and "If this slot is already taken" options;
  Save these parameters as a template after any booking attempt; a Booking Templates dashboard page to create,
  edit, duplicate, delete and book with templates.
- Samples with different parameters (sample sets) with element selection in every set.
- Edit User Inputs after booking: 1 minute to pay a higher charge; a lower charge is refunded to your wallet
  straight away if you edit before the cancellation deadline, and after the Officer In Charge's approval if later.
- Booking Assistant guided booking (Department -> Equipment -> inputs -> slot -> summary) with virtual booking IDs.
- Wallet: Transfer, Credit Facility and Recharge Wallet buttons together; Project Grant (SRIC) and Direct Cash
  Deposit / Bank Transfer recharge with OTP; Project Already Closed decline reason; approver named in SRIC emails.
- Student management: spending limits, identity card, Linked toggle, TA nominations; supervisor booking email.
- View Booking (renamed from Booking Management) with S.No., sorting and More filters; cancelled and refunded
  bookings keep their dates; Sync to calendar.
- OIC: Urgent booking final approval, Confirm manually on the waitlist, Change slot status week view (single-click
  arrows), view-only charges for all equipment, Tickets marked to me, daily completion reminders, important
  instruction per user type, walk-in equipment.
- Admin: Wallet payment modes switches, Experience ratings with Export CSV, New ticket email alerts, Booking
  Assistant Knowledge and Copilot Answers & Console, Legacy user sync.
- Smaller: server clock on the booking page, Back button on every page, larger home icon on the sign-in page,
  Sign in with email option for Channel i users, Lab In-charge renamed Lab Operator, Support tickets on the Lab
  Operator dashboard.
- Booking Assistant: instant answers for my bookings, wallet balance, recharge steps, transactions, results,
  waitlist and tickets, with next-step buttons on every booking (cancel / reschedule always need Confirm).
- Booking page: form kept after a failed booking with a restored draft, wallet link / Invite your supervisor
  banner and low-balance warning, remaining weekly quota, opening countdown, friendly waitlist queue message,
  tap a greyed slot to see why, phone-friendly slot grid and a Review line before Confirm; Need help? opens the
  Booking Assistant after a failed booking, and every answer has "Was this helpful?".
- My Bookings: cancel / reschedule deadline on each booking and a status legend.
- Sample sets start from the equipment's defaults; number fields start at 1 and show "Max N allowed".
- Catalog From prices, category filter and Book now; peak booking window (external users paused 8:55–9:15 PM on
  Wednesdays); keyboard-accessible dashboard menus.
- OIC: important instruction editor with fonts, point sizes and subscript / superscript. Admin: honorific titles
  for OICs and Lab Operators and the per-equipment Allow samples with different parameters switch.
""",
    },
    {
        "title": "Manage a Booking — Cancel, Reschedule, Edit, Message the Lab",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["cancel", "reschedule", "edit booking", "message the lab", "my bookings", "manage booking"],
        "external_url": "/my-bookings",
        "content_text": """
Open My Bookings (or ask the Booking Assistant "show my upcoming bookings") and pick the booking.
- Cancel / Reschedule: allowed for Pending or Booked bookings until the equipment's cutoff (48 hours before the
  slot unless the lab set another value). The refund or new slot is shown before you confirm. Repeat bookings
  created by the lab can't be changed by you. After the cutoff, use Message the lab or raise a support ticket.
- Once the lab has accepted your sample, Reschedule is no longer available to you or your supervisor; use Message
  the lab to contact the Officer in Charge.
- Edit parameters: choose Edit User Inputs on a Booked booking. A higher charge must be paid within 1 minute or
  the edit is undone. If the new charge is lower, the difference is refunded to your wallet straight away when
  you edit before the cancellation deadline; after that deadline the refund needs the Officer In Charge's approval.
- Message the lab: every booking has a Message the lab thread at the bottom of its details; lab staff reply in
  the same thread and you get a notification.
""",
    },
    {
        "title": "Results, Invoices and Ratings",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["results", "download results", "invoice", "proforma invoice", "rating", "feedback"],
        "external_url": "/my-results",
        "content_text": """
Results: when the lab publishes results you get an email and a notification. Open the booking in My Bookings or
My Results to view and download the files.
Invoice: open a completed booking and press Invoice (PDF) in its documents row. For a quote before booking or
payment use Proforma Invoice from the dashboard.
Rating: completed bookings on equipment with ratings switched on ask you to Rate this booking (overall rating and
a few yes/no questions); pending ratings are listed in My Bookings.
""",
    },
    {
        "title": "Waitlist and Urgent Requests (Users)",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["waitlist", "join waitlist", "leave waitlist", "urgent request", "my urgent requests"],
        "external_url": "/my-bookings",
        "content_text": """
Waitlist: on the booking page tick "Add to the waitlist if the booking cannot be completed". Your waitlist
entries appear in My Bookings; use Leave Waitlist on an entry to opt out (everyone behind you moves up). The OIC
can place a waitlisted booking into a free slot; the charge is then debited from the wallet.
Urgent requests: use Request urgent booking on the booking page (Type A rush relief or Type B urgent with reason,
50% surcharge). Track them under My Urgent Requests; students' Type B requests need supervisor approval first.
""",
    },
    {
        "title": "Support Tickets",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["ticket", "support ticket", "raise ticket", "complaint", "help desk"],
        "external_url": "/tickets",
        "content_text": """
Open Support Tickets from the user menu and press Create New Ticket (or Raise Support Request on an equipment
page). Describe the problem, include the booking ID if it is about a booking, and submit. You get email updates
and can reply in the ticket. The Booking
Assistant can also raise a ticket with the conversation attached — it only does so when you press Raise a
support ticket.
""",
    },
    {
        "title": "Staff Daily Queue — Today's Bookings, Approvals, Waitlist and Urgent Requests",
        "category": DocumentCategory.SOP,
        "security_level": SecurityLevel.OPERATOR,
        "tags": ["today's bookings", "pending approvals", "waitlist queue", "urgent requests", "role:operator",
                 "role:admin", "role:dept_admin"],
        "external_url": "/booking-management",
        "content_text": """
Lab Operators and OICs: ask the Booking Assistant "today's bookings on my equipment", "pending approvals",
"waitlist queue" or "urgent requests" for a live list scoped to the equipment you handle (department admins see
their department, admins all equipment). Each row opens the booking in View Booking. Approvals, manual
waitlist confirmation and urgent-request decisions are done on those pages, not in chat.
""",
    },
    {
        "title": "FESEM vs TEM — Quick Advisor",
        "category": DocumentCategory.EQUIPMENT,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["fesem", "tem", "advisor", "elemental mapping"],
        "content_text": """
Guidance (not a substitute for OIC advice):
- FESEM with EDS: surface morphology and elemental mapping; often suitable for metals, coatings, polymers (with care).
- TEM: internal nanostructure, crystallography, higher resolution; sample prep is more demanding (thin specimens).
- For grain size + elemental mapping on stainless steel, FESEM+EDS is commonly recommended first.
Always confirm sample preparation requirements and charges on the equipment page.
""",
    },
    {
        "title": "Remote Analysis Assistant Overview",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["remote analysis", "raa", "software"],
        "external_url": "/remote-analysis",
        "content_text": """
Remote Analysis lets you analyse your data on a reserved Analysis PC from a booking.
- Open the booking in My Bookings and press Open Analysis Workspace (shown when remote analysis is set up for
  that instrument).
- Choose your data: Current Booking Data, Previous Booking Data, or Upload additional files. They appear in the
  Input Data folder on the Analysis PC.
- Pick the software (the list is set by the lab for each instrument) and press Open Analysis Environment.
- Save outputs in the session workspace (Output folder). End Session uploads them to Booking Details → Analyzed
  Data; you get an email when they are ready (files are not emailed).
- Session time remaining is shown in the workspace. Analysis time is charged separately (Analysis charges).
- If connection fails, verify agent heartbeat and portal reachability; escalate with diagnostics if needed.
""",
    },
    {
        "title": "Your Data, Results Files and Analysis Software",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["data", "files", "results", "raw data", "analyzed data", "software", "imagej", "dm4", "pxrd",
                 "remote analysis", "download"],
        "external_url": "/my-results",
        "content_text": """
Where are my files? Raw Data and Analyzed Data for each booking are on its Booking Details page in My Bookings;
View results lists everything uploaded for you, newest first. The lab uploads results after the measurement and
you get an email (download from the portal; files are not emailed). Files created in a remote-analysis session
are uploaded to Booking Details → Analyzed Data when you press End Session. Data a colleague shared with you is
under Shared with me / My Research.
Which software can I use? The Analysis Workspace of your booking lists the software the lab set up for that
instrument (ask the Booking Assistant "what software can I use for <instrument>?"). In general, TEM .dm3/.dm4
files open in Gatan DigitalMicrograph or the free ImageJ/Fiji; XRD patterns are analysed with the vendor
software (for example HighScore or DIFFRAC.EVA) and free tools such as Profex, GSAS-II or FullProf for Rietveld
refinement.
""",
    },
    {
        "title": "Characterisation Terms — FWHM, Bragg's Law, XRD, PXRD, SEM, TEM",
        "category": DocumentCategory.EQUIPMENT,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["fwhm", "bragg", "scherrer", "xrd", "pxrd", "sem", "fesem", "tem", "eds", "glossary", "thin film"],
        "content_text": """
FWHM (full width at half maximum): the width of a peak at half its height. Broader XRD peaks mean smaller
crystallites and/or more strain; the Scherrer equation D = Kλ/(β cos θ) (β = FWHM in radians, K ≈ 0.9) estimates
crystallite size.
Bragg's law: nλ = 2d sin θ — each XRD peak angle gives the spacing d between crystal planes.
XRD: identifies crystal phases, lattice parameters, crystallite size, strain and texture. PXRD is XRD on a finely
ground powder, matched against reference databases (ICDD PDF, COD). Thin films are measured in grazing-incidence
mode (GIXRD) where the instrument supports it.
SEM / FESEM: images surface morphology; FESEM gives higher resolution at low voltage. Non-conductive samples
usually need a thin gold or carbon coating. EDS adds elemental composition and maps.
TEM: electrons pass through a very thin sample (< ~100 nm) to show internal structure, lattice fringes and
diffraction patterns.
XRD vs SEM: XRD tells you what crystal structure/phases are present (averaged over the sample); SEM shows what the
surface looks like (shape, size, texture) at a chosen spot.
""",
    },
    {
        "title": "Lab Access Hours and Sample Drop-off",
        "category": DocumentCategory.POLICY,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["lab hours", "access hours", "timings", "opening hours", "sample drop-off"],
        "external_url": "/availability",
        "content_text": """
There is no single set of opening hours for all labs: each instrument has its own slot timings set by its lab.
You can use an instrument during your booked slot; the booking page and the Availability page show its free
slots. Weekends, institute holidays and maintenance days are blocked automatically. Sample drop-off and
collection follow the instrument's sample-submission deadline (24 hours before the slot unless the lab set another
value; the previous working day if it falls on a weekend or holiday) and collection rules shown in its booking
rules. For access outside your slot, contact the instrument's operator or Officer in Charge (equipment page →
Contacts).
""",
    },
    {
        "title": "Preparing for Your Booking",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["prepare", "preparation", "sample preparation", "before booking", "checklist", "submission"],
        "content_text": """
Before your slot: read the instrument's sample instructions on its equipment page (the Booking Assistant shows them
with "what should I prepare before my <instrument> booking?"), have the required booking inputs ready, submit
your sample before the submission deadline shown in My Bookings, label samples clearly and declare anything
hazardous, air-sensitive or magnetic.
General tips (the lab's instructions take priority): XRD — fine, lump-free powder or a film on a flat substrate;
SEM/FESEM — completely dry, vacuum-safe, conductive coating for non-conductive samples; TEM — dispersed on a grid,
thinner than about 100 nm; XPS — dry, clean surface; BET — dry powder of known mass and its safe degassing
temperature.
""",
    },
    {
        "title": "Charges, Cost Estimates and PI Pricing",
        "category": DocumentCategory.POLICY,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["charges", "cost", "estimate", "price", "pi pricing", "wallet owner", "who pays", "gst"],
        "external_url": "/analysis-charges",
        "content_text": """
Charges come from the instrument's charge profile for the account category of the person making the booking; the
booking page shows the exact amount before you confirm, and the Booking Assistant can estimate N samples ("how
much do 5 XRD samples cost?"). PI rates apply when you, or the owner of the wallet you book from, are registered
as a PI of that instrument and the lab has set PI rates; otherwise the standard rate for your category applies (or
a discounted rate if your account was approved for one). Students' bookings are debited from the supervisor's
wallet. GST applies to external users only. The Analysis Charges page lists rates by instrument.
""",
    },
    {
        "title": "Linking to Your Supervisor's Wallet (Students)",
        "category": DocumentCategory.USER_GUIDE,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["link wallet", "join wallet", "supervisor", "faculty wallet", "join request", "student"],
        "external_url": "/wallet",
        "content_text": """
Students book from their supervisor's wallet. Open Wallet → Request to Join Wallet, search for your supervisor and
press Send Request; once they approve it (Student management), your bookings are charged to their wallet and any
spending limit they set applies. If your supervisor's name is not in the list, they have not signed in to the
portal yet: click Invite your supervisor on the Wallet page and enter their IIT Roorkee email;
when they sign in, the invitation becomes a normal link request they approve. Faculty can see and approve join
requests under Student management.
""",
    },
    {
        "title": "DSA and Equipment PC Zero-Touch (Department Admin)",
        "category": DocumentCategory.DEPLOYMENT,
        "security_level": SecurityLevel.DEPT_ADMIN,
        "tags": ["dsa", "equipment pc", "provisioning", "heartbeat"],
        "content_text": """
Department Sync Agent (DSA):
- Install → Portal Login → Trusted Auto-Approve → Finish.
- Verify Online status and heartbeat in Device Provisioning.
Equipment PC Wizard:
- Discover DSA → Login → Select unassigned equipment → READY.
No enrollment keys or UUIDs on the standard path.
Troubleshooting: check Lab LAN, DSA service, and portal Trusted policy.
""",
    },
    {
        "title": "Operator Sample Handling SOP (Summary)",
        "category": DocumentCategory.SOP,
        "security_level": SecurityLevel.OPERATOR,
        "tags": ["operator", "sample", "sop"],
        "content_text": """
Operator guidance (summary):
- Accept samples only after identity and booking verification.
- Update status to SAMPLE_ACCEPTED when ready to run.
- Use HOLD when clarification or payment is required.
- Upload results and complete booking per lab SOP.
Full operator manuals may be department-specific and higher security.
""",
    },
    {
        "title": "Cancellation and Urgent Booking Policy (Summary)",
        "category": DocumentCategory.POLICY,
        "security_level": SecurityLevel.AUTHENTICATED,
        "tags": ["cancellation", "urgent", "policy", "type a", "type b", "rush relief", "surcharge"],
        "content_text": """
Cancellation: cancel or reschedule from View Booking (My Bookings) until the equipment's cutoff (48 hours unless
the lab set another value) before the slot; partial cancellation of multi-slot bookings is allowed where enabled.
Refunds follow the cancellation policy and are shown before you confirm.
Urgent booking: on the booking page click Request urgent booking (students can also use Urgent booking request on
the dashboard) and choose a type:
- Type A — Rush relief (no surcharge): for internal users with at least 2 failed peak-window booking attempts in
  the last 14 days (quota-limit failures do not count). Book a slot in the advance week at normal rates; using
  Type A resets the 14-day window.
- Type B — Urgent with reason (50% surcharge): select slots, give a reason (at least 10 characters, optional
  supporting document) and accept the surcharge. Slots are held, not confirmed. Students need their supervisor's
  approval first; the Officer In Charge then gives final approval and may reschedule, including weekends. The
  wallet is charged only after final approval. Weekly caps may apply.
Urgent requests are separate from the waitlist and never cancel other users' confirmed bookings.
Maintenance and disruption: unavailable equipment shows maintenance or disruption messaging on the equipment page.
""",
    },
    {
        "title": "Admin Runbook — Research Copilot Knowledge Index",
        "category": DocumentCategory.DEPLOYMENT,
        "security_level": SecurityLevel.ADMIN,
        "tags": ["admin", "rag", "index", "runbook"],
        "content_text": """
Admin-only: Knowledge Center operations.
- Upload or create documents; set security level carefully.
- Rebuild index after bulk updates.
- After a release that changes these baseline articles, re-seed them with the management command
  seed_research_copilot_knowledge --force (or POST /api/v1/research-copilot/knowledge/seed/ with force=true);
  articles are matched by title and updated in place.
- Verified answers in Booking Assistant Knowledge take priority over these baseline articles; keep them consistent.
- Review Failed documents and Knowledge Gaps for FAQ candidates.
- Never auto-publish suggested FAQs without human review.
""",
    },
]


def seed_baseline_knowledge(*, force: bool = False) -> dict:
    created = updated = skipped = 0
    for article in SEED_ARTICLES:
        existing = KnowledgeDocument.objects.filter(title=article["title"]).first()
        if existing and not force:
            skipped += 1
            continue
        upsert_document(
            title=article["title"],
            content_text=article["content_text"],
            category=article["category"],
            security_level=article["security_level"],
            tags=article.get("tags") or [],
            external_url=article.get("external_url") or "",
            source_type="article",
            source_uri=f"seed://{article['title']}",
            document_id=existing.id if existing else None,
            index_now=True,
        )
        if existing:
            updated += 1
        else:
            created += 1
    return {"created": created, "updated": updated, "skipped": skipped}

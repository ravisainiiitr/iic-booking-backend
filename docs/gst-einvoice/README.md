# Portal GST invoicing of facility charges (sandbox)

This module implements the proposal to Dean SRIC, *Proposal for Automating GST Invoicing of Facility Charges through
the IIT Roorkee Equipment Booking Portal* (10–11 Nov 2026).

It is **sandbox only**:
- no call to the production GST portal;
- no real IRN;
- no live credential;
- no payment is touched.

The code is in the package `iic_booking/gst_einvoice/`, and the signing utility in `tools/sric_signer/`.

## How it works

```
booking completed / early request
  -> external_services.SricInvoiceRequest (snapshot: booking, billing, payments)          [existing]
  -> gst_einvoice: buyer, lines (SAC + rate from SRIC's tax rule), CGST+SGST / IGST, review rules
  -> rules pass (or SRIC approves) -> number from the portal series EBP/26-27/0001 (only when final)
       buyer has a valid GSTIN (B2B): e-invoice JSON (NIC schema 1.1) -> NIC sandbox / mock -> IRN + signed QR -> PDF
       no GSTIN (B2C): tax invoice PDF -> SRIC signatory signs with DSC on own computer -> portal checks -> signed PDF
  -> delivered as the request's OfficialInvoiceDocument (existing download, notification and printed-copy flow)
```

### Hook into the existing flow
- When a new `SricInvoiceRequest` is committed, it is offered to this module (`signals.py`, Celery task
  `gst_einvoice.prepare_request`).
- The task `gst_einvoice.process_pending` runs every 15 minutes. It picks up any request that was missed, and it
  re-checks e-invoice calls that got no answer.
- In the sandbox, only bookings made by **test accounts** are taken up.

### Weekly SRIC e-mail batch
- The batch skips requests that the portal invoices itself.
- If SRIC rejects the portal invoice, the request goes to SRIC by e-mail, as before (`sric_dispatch._portal_invoiced`).

### Tax
- The rule is SAC 998397 at 18%. It is a `TaxRule`, which the Main Administrator can edit; it is marked as subject
  to SRIC's approval.
- Supplier details: GSTIN 05AAALI0033R1Z5, PAN AAALI0033R, state code 05 (Uttarakhand).
- A buyer in the same state pays CGST + SGST; a buyer in another state pays IGST.
- The total is rounded to the rupee, with a round-off of at most ±99.99.

### Review rules
An invoice is held for SRIC's review, before a number is used, when any of these holds:
- the buyer's GSTIN is invalid, or the buyer's state is unknown;
- the total is above the review limit;
- the invoice total differs from the booking charge by more than ₹1;
- there is no payment receipt number;
- there is no active tax rule;
- automatic issue is switched off.

### Numbering
- An invoice is never renumbered. If Generate IRN gets no answer, the portal first calls *Get IRN by document*
  before it tries again.
- A refused invoice keeps its number. Correct the buyer and send it again.
- There is **no cancellation**; this was SRIC's decision.

### Invoice PDF (`pdf.py`)
- It has the payment receipt's look: navy and gold bands, the crest and the emblem watermark.
- It quotes the payment receipt number.
- It says "SANDBOX DEMONSTRATION – NOT A VALID TAX INVOICE".
- It is rendered once and stored with its SHA-256.
- On a B2C invoice, the signature box is at a fixed position (`dsc.SIGNATURE_BOX`).

### DSC check (`dsc.py`)
The portal accepts a signed PDF only when all of these hold:
- the file is the prepared PDF plus one incremental update;
- the update adds one signature, which covers the whole file;
- the CMS signature verifies with a certificate registered to the uploader, and that certificate is active and
  in date;
- the page content is unchanged;
- the only additions are signature widgets inside the signature box.

Every refusal is logged on the invoice.

### Register
- The endpoint is `/api/v1/gst-einvoice/register/?month=YYYY-MM`; add `&export=csv` for CSV.
- It lists every numbered invoice of the month, with totals.
- It also gives each series' first number, last number and count, for GSTR-1 Table 13.

## Configuration (environment only)

| Variable | Default | Meaning |
|---|---|---|
| `GST_EINVOICE_ENABLED` | off | Shows and runs the module |
| `GST_EINVOICE_MODE` | `sandbox` | Any other value switches the module **off**; there is no production mode |
| `GST_EINVOICE_PROVIDER` | `mock` | `mock` (simulated, nothing leaves the server) or `nic_sandbox` |
| `GST_EINVOICE_SANDBOX_BASE_URL` | `https://einv-apisandbox.nic.in` | Must be the NIC sandbox host over HTTPS. Production IRP hosts are refused before any request |
| `GST_EINVOICE_SANDBOX_CLIENT_ID` / `_CLIENT_SECRET` / `_USERNAME` / `_PASSWORD` / `_GSTIN` | – | NIC **sandbox** API credentials |
| `GST_EINVOICE_SANDBOX_PUBLIC_KEY` | – | NIC sandbox public key (PEM text or a file path) |
| `GST_EINVOICE_SANDBOX_APPKEY_BYTES` | 32 | Open NIC point: AppKey length, 32 or 16 |
| `GST_EINVOICE_SANDBOX_CLIENT_ID_HEADER` | `client_id` | Open NIC point: `client_id` or `client-id`. The secret header follows the same spelling |
| `GST_EINVOICE_SANDBOX_VITAL_PREFIX` | `/eivital/v1.04` | Open NIC point: path prefix for authentication and GSTIN |
| `GST_EINVOICE_SANDBOX_CORE_PREFIX` | `/eicore/v1.03` | Open NIC point: path prefix for invoice operations |

Secrets are never stored in the database and never sent to the browser. They are redacted from the call log.

## Roles

- **SRIC reviewers**: the Main Administrator, the Accounts In Charge (`finance`), and the accounts listed under SRIC
  emails in the settings. They see all invoices, approve held invoices or mark them not to be issued, correct
  buyers, send invoices again, and use the register.
- **Main Administrator**: sets the rules (review limit, automatic issue, SAC and rate) and registers the
  signatories' DSC certificates.
- **Signatory**: a user with an active registered certificate. Links the signing utility; the token is shown once.
- **Buyer**: sees the invoice on the booking (My Bookings → invoice) and downloads it as the official invoice.

The frontend page is `/accounts/gst-invoices`. It is linked from *GST invoices for external bookings*.

## Migration

- `gst_einvoice` 0001 creates the tables.
- `gst_einvoice` 0002 adds the SAC 998397 @ 18% rule and the 15-minute schedule.
- `gst_einvoice` 0003 adds credit notes (doc_type, original invoice, refund, reason) and the Main Administrator's credit-note permission table.
- Nothing else changes.

Deploy does not run migrations, so run the production migrations explicitly when this is released. The module stays
off until `GST_EINVOICE_ENABLED=true`.

## Demonstration (sandbox)

```bash
# 1. a test DSC (NOT a real DSC; for the demonstration only)
python tools/sric_signer/make_test_dsc.py --out test-dsc
# 2. demo data, signatory, signing utility token
GST_EINVOICE_ENABLED=true python manage.py gst_einvoice_demo --signatory-email sric.signer@iic-booking.test \
    --certificate test-dsc/test-dsc.cer --token-out test-dsc/token.txt --show-timeout
# 3. sign the B2C invoices with the utility (for the window, run it without --cli)
python tools/sric_signer/sric_signer.py --cli --portal http://localhost:8000 --token-file test-dsc/token.txt \
    --pkcs12 test-dsc/test-dsc.p12 --all
```

To register the B2B invoices on the NIC sandbox, set `GST_EINVOICE_PROVIDER=nic_sandbox` and the sandbox credentials,
then run the same steps.

## GSTR-1 export (B2C invoices)

The portal builds the GSTR-1 data for invoices to buyers **without a GSTIN** (`gstr1.py`). E-invoices are left out:
the GST system fills them into GSTR-1 from the IRP.

### Where to get it
- In the portal: GST invoices → Monthly register → *GSTR-1 export*.
- In the SRIC Invoice Signer: the *Monthly export* row.
- API: `GET /api/v1/gst-einvoice/gstr1/?from=YYYY-MM-DD&to=YYYY-MM-DD&kind=preview|json|xlsx|invoice_register|credit_note_register|all` (SRIC reviewers), or
  `/signer/api/gstr1/` (the signing utility's device token).

### Files
- There is one file per calendar month, with `fp` = MMYYYY, because SRIC files monthly. There is no quarterly or IFF
  option.
- A range over several months downloads as a zip, with one file per month.
- JSON: the offline tool's upload format (`gstin`, `fp`, `b2cs`, `b2cl`, `cdnur`, `hsn.hsn_b2c`, `doc_issue`).
- Excel (`xlsx`): the offline tool's template sheets `b2cl`, `b2cs`, `cdnur`, `hsn(b2c)` and `docs`, plus a *read me*
  sheet.
- **Invoice Register** (`invoice_register`, `registers.py`): one row per B2C tax invoice of the month: number, date,
  status, whether it is in the JSON, GSTR-1 table (B2CS/B2CL), buyer and address, place of supply, intra/inter-state,
  SAC, rate, taxable value, CGST, SGST, IGST, round-off, invoice value, receipt number and date, booking, FBR,
  equipment, facility, signed on/by, DSC certificate serial, credit notes issued against it, credited and net value.
- **Credit Note Register** (`credit_note_register`): one row per B2C credit note of the month: number, date, status,
  whether it is in the JSON, GSTR-1 treatment (CDNUR or netted in B2CS), the original invoice's number, date, value,
  return period and table, buyer, place of supply, taxes, reason, refund, receipt, booking, signed on/by, DSC serial.
- **All three** (`all`): GSTR-1 JSON, Invoice Register and Credit Note Register of each month in one zip.
- The registers list every numbered document, including those still waiting for the DSC (marked *not in the JSON*);
  their totals row adds up only the rows the JSON contains, so they reconcile with it.

### Contents
| Table | What goes in it |
|---|---|
| **B2CS (Table 7)** | Every intra-state B2C supply, and inter-state B2C invoices up to ₹1,00,000, by place of supply and rate |
| **B2CL (Table 5)** | Inter-state B2C invoices above ₹1,00,000 (the limit from the August 2024 tax period), invoice by invoice |
| **CDNUR (Table 9B)** | Credit notes against B2CL invoices |
| **HSN (Table 12, B2C tab)** | SAC 998397 by rate. Services are reported with UQC `NA` and quantity 0 |
| **Documents issued (Table 13)** | The month's whole invoice series, B2B e-invoices included, and the credit-note series: from, to, total, and 0 cancelled |

Credit notes against B2CS supplies are netted into B2CS in the month the note is issued.

Credit notes are issued only where the Main Administrator allowed one for an invoice (see *Credit notes on refund*);
in a month without any, the CDNUR section is empty and the export says so.

Only invoices signed with the DSC count as issued. Numbered B2C invoices still waiting for the signature are left out,
and the export lists them as a warning.

### To confirm with SRIC before the first filing
GSTN publishes no formal schema. The items below sit in `gstr1.FORMAT` and can be adjusted:
- the JSON `version` string of the offline tool SRIC uses;
- the exact Excel template headers;
- UQC `NA` for services. The original brief said `OTH`; GSTN's advisory says `NA`.

The ₹1 lakh limit is `gstr1.B2CL_LIMIT`.

## Credit notes on refund (Main Administrator, case by case)

### When no credit note is needed
- A booking's tax invoice is issued after the work is done, so a refund normally never meets an invoice.
- A full refund more than 48 hours before the slot goes to the wallet before any invoice exists.
- Both stay as they were.

### When a refund meets an invoice
This happens in two cases:
- **Advance invoice.** The user may already ask for the invoice before the work: *Request the GST invoice before
  analysis is completed* (`external_services` `InvoiceOption.EARLY`). Before confirming, they must accept the
  early-invoice disclosure, which says that if the work cannot be done, only the base amount may come back. The
  acceptance is recorded on the booking's `InvoicePreference` with the policy version, a hash of the text and the
  time. The option is off until `ExternalServicesSettings.early_invoice_allowed` is switched on, and the disclosure
  wording still awaits SRIC's approval.
- **Lab-failure refund after completion.**

Without a credit note, such a refund leaves the GST out (`refunds.quote`), as before.

### Allowing a credit note
- **Who and how:** only the **Main Administrator**, for one issued portal invoice, with a reason. In the portal:
  GST invoices → invoice → *Allow a credit note on refund*. The API is
  `POST /api/v1/gst-einvoice/invoices/<id>/credit-note-permission/`, and `.../revoke/` withdraws it.
- **Audit:** the decision is written to the invoice's append-only history.
- **No global switch:** without this decision, nothing changes.

### What happens on refund
Once allowed, the refund returns the GST too, and recording the refund (`refunds._record`) issues the credit note.
- **Series:** its own continuous series, **ECN/26-27/0001**. The suggested `EBP-CN/26-27/0001` is longer than the 16
  characters GST allows for a document number.
- **Reference:** it names the original invoice's number and date, in the PDF and in the e-invoice `PrecDocDtls`.
- **Amounts:** the taxable value and the CGST/SGST or IGST split follow the invoice. A partial refund gets a pro-rata
  credit note, and the total credited never exceeds the invoice.
- **Buyer with a GSTIN:** registered as a **CRN** e-invoice with the sandbox / mock.
- **No GSTIN:** a PDF signed with SRIC's DSC through the signer utility.
- The buyer is told, and downloads the credit note with the booking.

### In GSTR-1
- **CDNUR:** credit notes against B2CL invoices.
- **B2CS:** other B2C credit notes are netted into B2CS in the month of the note.
- **Table 13:** the credit-note series.
- **B2B credit notes:** like B2B invoices, they reach GSTR-1 (CDNR) from the e-invoice system, so the export leaves
  them out.

Migration: `gst_einvoice` 0003 (credit-note fields on the invoice, plus the permission table).

## Signatory: install and first run

1. Install the DSC token's own driver software (for example SafeNet Authentication Client, ePass2003, ProxKey).
2. The Main Administrator registers the signatory's certificate (.cer) under GST invoices → Signing (DSC).
3. Run `SRIC-Invoice-Signer-Setup-<version>.exe`. It is a per-user install, with Start-menu and desktop shortcuts and
   an uninstaller. It is not code-signed yet, so SmartScreen may ask for confirmation.
4. In the portal, under GST invoices → Signing (DSC) → *Link a signing utility*, copy the token shown (once). In the
   signer, enter the portal address and the token, then click **Connect**.
5. Plug in the DSC token. The signer finds the drivers of the common Indian DSC tokens (SafeNet/eToken, ePass2003,
   ProxKey/Watchdata, mToken, Gemalto/Thales, HyperPKI, Trust Key, Bit4id, SafeSign, Longmai) and lists the tokens
   that are plugged in, with their certificates. Choose yours. If it is not found, use **Browse for driver...**.
6. Select the invoices, preview them, enter the PIN and click **Sign selected with DSC**.

Details and messages: `tools/sric_signer/README.md`.

### Getting the installer
- Build it with the workflow *Build SRIC Invoice Signer*, which runs on pull requests that touch
  `tools/sric_signer`, or by hand.
- Download the artifact `sric-invoice-signer-windows`. It contains the exe, the installer and the exe's self-test
  report.
- Nothing is published as a release, and nothing is code-signed.

### What has been tested
- Signing through PKCS#11 has been tested against SoftHSM2, a software token, in CI. The portal's own verifier
  accepts the result.
- Token detection has been tested with a simulated Windows folder tree.
- Signing with a real USB DSC token has **not** been verified. Do it once with SRIC's token before the pilot.

## What SRIC and NIC must still provide

1. **NIC e-invoice sandbox access** for SRIC's GSTIN:
   - client ID and secret;
   - API username and password;
   - the sandbox public key.

   Then confirm the three open points (AppKey length, header spelling, path prefixes) at the first authentication.
2. **SRIC decisions** (proposal section 9):
   - approval of the portal series;
   - the SAC and rate for each service;
   - the review limit;
   - the names of the signatories;
   - the reviewers' accounts.
3. **Signatory setup:**
   - each signatory's DSC certificate (.cer, public part only), for registration in the portal;
   - the signing utility, installed on the signatory's institute computer;
   - the token driver's PKCS#11 library (eToken, ePass2003, ProxKey, ...) on that computer.

   The utility is a Windows installer, `SRIC-Invoice-Signer-Setup-<version>.exe` (see *Signatory: install and first
   run* below). It needs no Python and no administrator rights.
4. **Before any live use** (outside this change):
   - the tax consultant's confirmations (proposal section 11);
   - a production API user;
   - CRL/OCSP checking of DSC certificates;
   - an independent review of the signing utility;
   - the user's explicit go-ahead.

   This module has no production mode. Adding one is a separate change, with its own review.

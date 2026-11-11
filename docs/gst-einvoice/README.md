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

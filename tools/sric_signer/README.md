# SRIC Invoice Signer

The SRIC Invoice Signer signs the portal's tax invoices for buyers without a GSTIN. It uses the SRIC signatory's DSC
and runs on the signatory's own computer (Dean SRIC proposal, section 4 and Figure 3).

## How to sign

1. The window lists the invoices waiting for signature. Tick the ones to sign.
2. Click **Preview selected** to check them.
3. Enter the token PIN and click **Sign selected with DSC**.

## What keeps it safe

- The PIN goes only to the DSC token. The signing key never leaves the token.
- Before signing, the utility checks each downloaded invoice against the SHA-256 the portal listed.
- Only signed PDFs are uploaded. The portal accepts one only if both hold:
  - it verifies with your registered certificate;
  - it is the prepared invoice, unchanged.

## Linking the utility to the portal

In the portal, go to GST invoices → Signing (DSC) → *Link a signing utility*, and paste the token into the utility.
The token is shown only once. If the computer is lost, revoke the token on the same page.

```bash
pip install -r requirements.txt
python sric_signer.py                    # window
python sric_signer.py --cli --portal https://equip.iitr.ac.in --token-file token.txt --pkcs11-lib "C:\Windows\System32\eTPKCS11.dll" --all
```

## Test DSC (sandbox only)

- `--pkcs12` signs with a key file. It exists **only** for the sandbox demonstration, with a test DSC made by
  `make_test_dsc.py`.
- The utility refuses `--pkcs12` unless the portal reports sandbox mode.

# SRIC Invoice Signer

The SRIC Invoice Signer is a Windows program. It signs the portal's tax invoices for buyers without a GSTIN, using
the SRIC signatory's DSC token, on the signatory's own computer (Dean SRIC proposal, section 4 and Figure 3).

- The PIN goes only to the DSC token. The signing key never leaves the token.
- Before signing, the utility checks each downloaded invoice against the SHA-256 the portal listed. It signs only
  the invoice the portal prepared.
- It uploads only signed PDFs. The portal accepts a signed PDF only if both hold:
  - it verifies with the signatory's registered certificate;
  - it is the prepared invoice, unchanged.

## For the signatory: install and first run

1. **The DSC token's own software.** Install the driver software that came with your token (for example SafeNet
   Authentication Client, the ePass2003 or ProxKey installer). The signer uses that software's PKCS#11 driver.
2. **Install the signer.** Run `SRIC-Invoice-Signer-Setup-<version>.exe`.
   - It installs for your Windows account only, with no administrator rights needed.
   - It adds a Start-menu entry and, if you tick the option, a desktop shortcut.
   - To uninstall, use Settings → Apps, or *Uninstall SRIC Invoice Signer* in the Start menu.
   - The installer and the exe are not code-signed yet, so Windows SmartScreen may warn. Choose *More info → Run
     anyway* only if the file came from the portal team.
3. **Link it to the portal (once).**
   - In the portal, open GST invoices → **Signing (DSC)** → *Link a signing utility*. You need a registered DSC
     certificate for this step.
   - Copy the token shown there. It is shown only once.
   - Open the SRIC Invoice Signer, enter the portal address (`https://equip.iitr.ac.in`), paste the token into
     *Utility token* and click **Connect**.
4. **Pick your DSC token.**
   - Plug in the token. The signer looks for the drivers of the common Indian DSC tokens when it starts; click
     **Detect token** to look again.
   - Choose your certificate in the list. Each entry shows the token's label and the certificate holder's name.
   - If your token is not found, click **Browse for driver...** and choose its PKCS#11 DLL. The table below lists
     the usual file names.
   - The signer remembers the token and certificate you chose.
5. **Sign.**
   - The list shows the invoices waiting for your signature. Select the ones to sign.
   - Click **Preview selected** to read them.
   - Enter the token PIN and click **Sign selected with DSC**.
   - The signed invoices go to the portal, and the buyers can then download them.

If the screen says:
- **No DSC token is plugged in:** insert the token, wait for its light, and click **Detect token**.
- **The PIN is wrong:** check the PIN before trying again. The token locks itself after a few wrong PINs, and the
  signer stops at the first wrong PIN instead of trying it on every invoice.
- **The token is locked:** it must be unlocked with its admin (SO) PIN, using the token vendor's software or the DSC
  issuer's help.

**Driver architecture:** the signer is 64-bit, so it uses 64-bit drivers. Token installers normally add both
versions: 64-bit in `C:\Windows\System32`, 32-bit in `C:\Windows\SysWOW64`. *Detect token* lists drivers of the
other architecture separately, so you can see why they are not used.

### Token drivers the signer looks for

It looks in System32, SysWOW64 and the token vendors' folders under Program Files:

| Token | Driver file(s) |
|---|---|
| SafeNet / eToken | `eTPKCS11.dll` |
| ePass2003 | `eps2003csp11.dll`, `eps2003csp11v2.dll` |
| ProxKey / Watchdata | `SignatureP11.dll`, `WDPKCS.dll` |
| mToken | `mtoken_pkcs11.dll`, `CryptoIDA_pkcs11.dll` |
| Gemalto / Thales IDPrime | `IDPrimePKCS11.dll`, `gclib.dll` |
| HyperPKI | `hypersecurep11.dll` |
| Trust Key | `TrustKeyP11.dll` |
| Bit4id | `bit4xpki.dll` |
| SafeSign | `aetpkss1.dll` |
| Longmai | `mtoken_gm3000.dll`, `gm3000_pkcs11.dll`, `longmai_pkcs11.dll`, `lmpkcs11.dll` |

The Longmai file names are a best guess. If yours differs, use **Browse for driver...**; the choice is remembered.
To list what the signer finds, use the Start-menu entry *SRIC Invoice Signer - detect DSC token*.

Saved settings are kept in `%APPDATA%\SRIC Invoice Signer\config.json`: the portal address, the utility token, and
the chosen driver, token and certificate. The PIN is never saved. If the computer is lost, revoke the utility token
in the portal.

## Test DSC (sandbox only)

- `--pkcs12` (or *Test DSC file*) signs with a key file. It exists **only** for the sandbox demonstration, with a
  test DSC made by `make_test_dsc.py`.
- The utility refuses it unless the portal reports sandbox mode.

## For developers

```
tools/sric_signer/
  sric_signer.py      the program (window, --cli, --detect, --self-test)
  sric_token.py       driver search, token listing, plain-language token errors
  version.py          version used by the exe resource and the installer
  assets/             icon (from fonts/IITR_Logo.png via packaging/make_icon.py)
  packaging/          PyInstaller spec, version resource, Inno Setup script, build.ps1
  tests/              detection tests (mocked) and a real PKCS#11 run against SoftHSM2
```

```powershell
# from source
pip install -r requirements.txt -r packaging\build-requirements.txt
python -m pytest                         # in tools/sric_signer; the SoftHSM2 tests run where SoftHSM2 is installed
python sric_signer.py                    # window
python sric_signer.py --detect           # drivers and tokens found
# exe + installer (needs Inno Setup 6 for the installer)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The workflow **Build SRIC Invoice Signer** (`.github/workflows/build-sric-signer.yml`) runs on pull requests that
touch this folder, and can also be started by hand. It has two jobs:
- **On ubuntu-latest:** the tests, including the real PKCS#11 signing run against SoftHSM2. That run is checked by
  pyHanko and by the portal's own verifier.
- **On windows-latest:** the exe and the installer, plus the exe's self-test.

The exe and the installer are uploaded as the workflow artifact `sric-invoice-signer-windows`. There is no release
publishing and no code signing.

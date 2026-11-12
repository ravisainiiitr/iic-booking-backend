# PyInstaller spec: one windowed exe, "SRIC Invoice Signer.exe", with pyHanko and python-pkcs11 bundled (no Python
# needed on the signatory's computer). Build with packaging/build.ps1.
import os

from PyInstaller.utils.hooks import collect_all, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [(os.path.join(ROOT, "assets", "sric_signer.ico"), "assets")]
binaries, hiddenimports = [], ["sric_token", "version", "tkinter", "tkinter.ttk", "tkinter.filedialog",
                               "tkinter.messagebox"]
for package in ("pyhanko", "pyhanko_certvalidator", "pkcs11", "asn1crypto", "tzlocal", "uritools", "qrcode"):
    try:
        d, b, h = collect_all(package)
    except Exception:  # an optional dependency that is not installed
        continue
    datas += d
    binaries += b
    hiddenimports += h
for dist in ("pyHanko", "pyhanko-certvalidator", "python-pkcs11", "requests"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

a = Analysis([os.path.join(ROOT, "sric_signer.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, excludes=["pytest", "IPython", "matplotlib", "numpy"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="SRIC Invoice Signer", console=False, upx=False,
          icon=os.path.join(ROOT, "assets", "sric_signer.ico"), version=os.path.join(SPECPATH, "version_info.txt"))

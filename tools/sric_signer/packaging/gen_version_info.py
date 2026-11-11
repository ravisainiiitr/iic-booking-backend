"""Writes packaging/version_info.txt (the exe's Windows version resource) from version.py."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from version import APP_NAME, DESCRIPTION, PUBLISHER, VERSION  # noqa: E402

parts = tuple(int(p) for p in (VERSION.split(".") + ["0", "0", "0"])[:4])
TEMPLATE = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={parts}, prodvers={parts}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', {PUBLISHER!r}),
      StringStruct('FileDescription', {DESCRIPTION!r}),
      StringStruct('FileVersion', {VERSION!r}),
      StringStruct('InternalName', 'sric_signer'),
      StringStruct('LegalCopyright', {PUBLISHER!r}),
      StringStruct('OriginalFilename', {APP_NAME + '.exe'!r}),
      StringStruct('ProductName', {APP_NAME!r}),
      StringStruct('ProductVersion', {VERSION!r})])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
with open(os.path.join(HERE, "version_info.txt"), "w", encoding="utf-8") as fh:
    fh.write(TEMPLATE)
print(VERSION)

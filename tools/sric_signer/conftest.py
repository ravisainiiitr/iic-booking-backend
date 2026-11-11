"""The signer's tests need its own dependencies (pyHanko, python-pkcs11). In the backend's environment, where those
are not installed, the repository-wide pytest run skips them instead of failing to import."""
import importlib.util

collect_ignore_glob = [] if all(importlib.util.find_spec(m) for m in ("pkcs11", "pyhanko")) else ["tests/*"]

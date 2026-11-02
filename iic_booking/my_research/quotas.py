"""My Research storage quotas, set by the Main Administrator (Admin Settings > My Research storage).

A user's quota is their own one if the administrator set one, else the default per-user quota (100 GB unless
changed). A workspace quota is optional. 0 always means no limit.
"""

from __future__ import annotations

GB = 1024**3


def user_quota_bytes(user) -> int:
    from .models import StorageQuotaSettings, UserStorageQuota

    own = UserStorageQuota.objects.filter(user_id=getattr(user, "pk", None)).values_list("quota_gb", flat=True).first()
    gb = own if own is not None else StorageQuotaSettings.get_solo().user_quota_gb
    return int(gb) * GB


def workspace_quota_bytes() -> int:
    from .models import StorageQuotaSettings

    return int(StorageQuotaSettings.get_solo().workspace_quota_gb) * GB

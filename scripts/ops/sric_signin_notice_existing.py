# SRIC recharges already waiting for a faculty member who has not signed in to the new portal: send the
# Channel i sign-in email once (one email per faculty member). Rows already emailed and test rows are skipped.
# APPLY=1 sends; otherwise dry run. Run via `manage.py shell`.
import os

from iic_booking.users.models.sric_wallet_recharge import SricWalletRecharge, SricWalletRechargeStatus
from iic_booking.users.sric_signin_notice import find_notice_address, needs_signin_notice, send_new_row_notices

apply = os.environ.get("APPLY") == "1"
print(f"mode={'APPLY' if apply else 'DRY RUN'}")


def masked(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "-"


rows = [
    r
    for r in SricWalletRecharge.objects.select_related("matched_user", "receiver_mapping")
    .filter(status=SricWalletRechargeStatus.NEEDS_REVIEW, is_test=False, signin_notice_sent_at__isnull=True)
    .order_by("pk")
    if needs_signin_notice(r)
]
print(f"waiting_rows={len(rows)}")
for r in rows:
    address, source = find_notice_address(r)
    print(f"{r.reference} emp={r.employee_id} reason={r.review_reason} to={masked(address)} source={source or 'none'}")

if apply and rows:
    print(f"result={send_new_row_notices([r.pk for r in rows])}")

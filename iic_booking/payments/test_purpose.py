"""Payment purpose wording shown on the payment page, in Checkout and on receipts."""

from iic_booking.payments.purpose import booking_purpose
from iic_booking.payments.purpose import recharge_purpose


def test_wording_by_charge_profile():
    assert booking_purpose("Powder XRD", "SAMPLE") == "Analysis charges for 'Powder XRD'"
    assert booking_purpose("Powder XRD", "HOUR") == "Analysis charges for 'Powder XRD'"
    assert booking_purpose("Ultimaker S5", "PRINT_3D") == "Fabrication charges for 'Ultimaker S5'"
    assert booking_purpose("Laser Cutter", "laser_cut_2d") == "Fabrication charges for 'Laser Cutter'"
    assert booking_purpose("", "") == "Analysis charges"


def test_wallet_recharge_wording():
    assert recharge_purpose("Physics") == "Wallet recharge for the Physics sub-wallet"
    assert recharge_purpose("") == "Wallet recharge"

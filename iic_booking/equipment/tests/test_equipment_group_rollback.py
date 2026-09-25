"""
Rollback safety: with the Equipment Group flags off (or a group switch off) the portal behaves exactly as
before, and data written while the features were on stays valid without any database change.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from rest_framework.response import Response

from iic_booking.equipment import equipment_group_service as egs


@pytest.mark.django_db
def test_flags_off_book_endpoint_is_a_direct_call(egs_factory, egs_flags_off):
    group = egs_factory.group(alternative_booking_enabled=True, auto_allocation_enabled=True)
    eq = egs_factory.equipment(group)
    user = egs_factory.student()
    sentinel = Response({"sentinel": True}, status=418)

    with patch("iic_booking.equipment.api_views._book_equipment_impl", return_value=sentinel) as impl, \
            patch.object(egs, "find_alternatives") as finder:
        res = egs_factory.client_for(user).post(
            f"/api/equipments/{eq.pk}/book/", {"slot_ids": [1], "offer_group_alternatives": True}, format="json"
        )

    assert res.status_code == 418 and res.data == {"sentinel": True}
    impl.assert_called_once()
    finder.assert_not_called()


@pytest.mark.django_db
def test_flags_off_waitlist_decision_unchanged(egs_factory, egs_flags_off):
    from iic_booking.equipment.api_views import _enrich_failed_booking_response

    group = egs_factory.group(alternative_booking_enabled=True)
    eq = egs_factory.equipment(group, waitlist_queue_depth=5)
    user = egs_factory.student()

    def impl(request, pk):
        return Response(
            _enrich_failed_booking_response(
                eq, request.user, "Slot unavailable", waitlist_on_failure=True, slot_unavailable_failure=True
            ),
            status=400,
        )

    with patch("iic_booking.equipment.api_views.add_user_to_waitlist", return_value=(True, 1)) as add_wl, \
            patch("iic_booking.equipment.api_views._book_equipment_impl", side_effect=impl), \
            patch.object(egs, "find_alternatives") as finder:
        res = egs_factory.client_for(user).post(
            f"/api/equipments/{eq.pk}/book/", {"slot_ids": [1], "offer_group_alternatives": True}, format="json"
        )
    assert res.status_code == 400
    add_wl.assert_called_once()
    finder.assert_not_called()
    assert res.data.get("code") != egs.ALTERNATIVES_AVAILABLE_CODE


@pytest.mark.django_db
def test_flags_off_cross_reschedule_rejected_and_booking_untouched(egs_factory, egs_flags_off, egs_quiet_side_effects):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, source, egs_factory.future(days=4))
    slot = egs_factory.slot(target, egs_factory.future(days=5, hour=11))

    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {"start_time": slot.start_datetime.isoformat(), "end_time": slot.end_datetime.isoformat(),
         "target_equipment_id": target.pk},
        format="json",
    )
    assert res.status_code == 400
    assert res.data["code"] == "CROSS_RESCHEDULING_DISABLED"
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk
    slot.refresh_from_db()
    assert slot.booking_id is None

    same = egs_factory.slot(source, egs_factory.future(days=5, hour=14))
    ok = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {"start_time": same.start_datetime.isoformat(), "end_time": same.end_datetime.isoformat()},
        format="json",
    )
    assert ok.status_code == 200, ok.data


@pytest.mark.django_db
def test_env_on_but_group_switch_off_behaves_as_off(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=False, cross_rescheduling_enabled=False)
    eq = egs_factory.equipment(group)
    user = egs_factory.student()
    sentinel = Response({"sentinel": True}, status=418)

    with patch("iic_booking.equipment.api_views._book_equipment_impl", return_value=sentinel) as impl:
        res = egs_factory.client_for(user).post(
            f"/api/equipments/{eq.pk}/book/", {"offer_group_alternatives": True}, format="json"
        )
    assert res.status_code == 418
    impl.assert_called_once()
    assert egs.cross_rescheduling_enabled(eq) is False


@pytest.mark.django_db
def test_data_written_while_on_stays_valid_after_flags_off(egs_factory, egs_flags_on, egs_quiet_side_effects):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, source, egs_factory.future(days=4))
    slot = egs_factory.slot(target, egs_factory.future(days=5, hour=11))
    moved = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {"start_time": slot.start_datetime.isoformat(), "end_time": slot.end_datetime.isoformat(),
         "target_equipment_id": target.pk},
        format="json",
    )
    assert moved.status_code == 200, moved.data

    egs_flags_on.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED = False
    egs_flags_on.EQUIPMENT_GROUP_CROSS_RESCHEDULING_ENABLED = False

    opts = egs_factory.client_for(owner).get(f"/api/bookings/{booking.pk}/reschedule-options/")
    assert opts.status_code == 200
    assert opts.data["cross_rescheduling_enabled"] is False
    assert [o["equipment_id"] for o in opts.data["options"]] == [target.pk]

    again = egs_factory.slot(target, egs_factory.future(days=6, hour=11))
    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {"start_time": again.start_datetime.isoformat(), "end_time": again.end_datetime.isoformat()},
        format="json",
    )
    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.equipment_id == target.pk


@pytest.mark.django_db
def test_flags_off_serializer_hides_features(egs_factory, egs_flags_off):
    from iic_booking.equipment.serializers import EquipmentDetailSerializer

    group = egs_factory.group(alternative_booking_enabled=True, cross_rescheduling_enabled=True)
    eq = egs_factory.equipment(group)
    serializer = EquipmentDetailSerializer()
    assert serializer.get_group_alternatives_enabled(eq) is False
    assert serializer.get_group_cross_reschedule_enabled(eq) is False

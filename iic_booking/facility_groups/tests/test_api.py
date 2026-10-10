import pytest

from iic_booking.facility_groups.models import FacilityUserGroup, FacilityUserGroupMember, GroupKind
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, make_booking, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def booked(world, run_on_commit):
    run_on_commit(make_booking, world.student, world.fesem)
    run_on_commit(make_booking, world.external, world.fesem)
    run_on_commit(make_booking, world.external, world.tem)
    world.category_group = FacilityUserGroup.objects.get(auto_key=f"category:{world.em.pk}")
    return world


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/"),
        ("get", "/options/"),
        ("post", "/"),
        ("post", "/email/preview/"),
        ("post", "/email/send/"),
        ("post", "/email/test/"),
        ("get", "/email/campaigns/"),
    ],
)
@pytest.mark.parametrize("user_type", [UserType.DEPT_ADMIN, UserType.MANAGER, UserType.FACULTY])
def test_only_main_admin(world, method, path, user_type):
    user = make_user(user_type=user_type, department=world.chem)
    res = getattr(client_for(user), method)(f"{API}{path}", {}, format="json")
    assert res.status_code == 403


def test_anonymous_rejected(world):
    assert client_for().get(f"{API}/").status_code in (401, 403)


def test_list_groups_with_counts(booked):
    res = client_for(booked.admin).get(f"{API}/")
    assert res.status_code == 200
    rows = {r["name"]: r for r in res.data["results"]}
    assert rows["Electron Microscopy"]["member_count"] == 2
    assert rows["Electron Microscopy"]["kind"] == GroupKind.CATEGORY
    # Student's supervisor is recorded but counted separately.
    assert rows["Electron Microscopy"]["supervisor_count"] == 1
    assert rows["All booking users"]["member_count"] == 2


def test_members_table_filters_and_department_breakdown(booked):
    admin = client_for(booked.admin)
    url = f"{API}/{booked.category_group.pk}/members/"
    res = admin.get(url)
    assert res.data["count"] == 2
    ext = next(r for r in res.data["results"] if r["user_id"] == booked.external.pk)
    assert ext["audience"] == "external"
    assert ext["department_name"] == "Delhi University"
    assert ext["booking_count"] == 2
    assert {e["name"] for e in ext["equipment"]} == {"FE-SEM", "TEM"}

    assert admin.get(url, {"audience": "internal"}).data["count"] == 1
    assert admin.get(url, {"department_ids": str(booked.org.pk)}).data["count"] == 1
    assert admin.get(url, {"user_types": UserType.STUDENT}).data["count"] == 1
    assert admin.get(url, {"search": "guest"}).data["count"] == 1
    assert admin.get(url, {"include_supervisors": "true"}).data["count"] == 3
    assert admin.get(url, {"audience": "nobody"}).status_code == 400

    breakdown = admin.get(f"{API}/{booked.category_group.pk}/departments/").data
    assert (breakdown["internal"], breakdown["external"]) == (1, 1)
    assert {d["department_name"] for d in breakdown["departments"]} == {"Chemistry", "Delhi University"}


def test_staff_without_external_type_counts_as_internal(world, run_on_commit):
    nobody = make_user(user_type=None, name="No Type")
    run_on_commit(make_booking, nobody, world.xrd)
    group = FacilityUserGroup.objects.get(auto_key=f"equipment:{world.xrd.pk}")
    res = client_for(world.admin).get(f"{API}/{group.pk}/members/", {"audience": "internal"})
    assert [r["user_id"] for r in res.data["results"]] == [nobody.pk]


def test_csv_export(booked):
    res = client_for(booked.admin).get(f"{API}/{booked.category_group.pk}/members/export/")
    assert res.status_code == 200
    assert res["Content-Type"].startswith("text/csv")
    body = res.content.decode("utf-8-sig")
    assert body.splitlines()[0].startswith("Name,Email,Mobile")
    assert "Guest Researcher" in body and "Delhi University" in body and "External" in body


def test_test_accounts_and_inactive_users_hidden_by_default(world, run_on_commit):
    tester = make_user(user_type=UserType.STUDENT, department=world.chem, is_test_account=True)
    run_on_commit(make_booking, tester, world.xrd)
    group = FacilityUserGroup.objects.get(auto_key=f"equipment:{world.xrd.pk}")
    admin = client_for(world.admin)
    assert admin.get(f"{API}/{group.pk}/members/").data["count"] == 0
    assert admin.get(f"{API}/{group.pk}/members/", {"include_test_accounts": "1"}).data["count"] == 1


def test_custom_group_lifecycle(booked):
    admin = client_for(booked.admin)
    res = admin.post(f"{API}/", {"name": "NMR workshop", "description": "Invitees"}, format="json")
    assert res.status_code == 201
    gid = res.data["id"]
    assert admin.post(f"{API}/", {"name": "nmr workshop"}, format="json").status_code == 400

    res = admin.post(f"{API}/{gid}/members/add/", {"user_ids": [booked.operator.pk]}, format="json")
    assert res.data == {"added": 1, "already_members": 0}
    res = admin.post(
        f"{API}/{gid}/members/add/",
        {"filters": {"audience": "external"}, "source_group_ids": [booked.category_group.pk]},
        format="json",
    )
    assert res.data["added"] == 1
    res = admin.post(f"{API}/{gid}/members/add/", {"filters": {"department_ids": [booked.chem.pk]}}, format="json")
    assert res.data["added"] >= 2  # student and faculty of Chemistry
    assert admin.get(f"{API}/{gid}/members/").data["count"] == FacilityUserGroupMember.objects.filter(group_id=gid).count()

    res = admin.post(f"{API}/{gid}/members/remove/", {"user_ids": [booked.operator.pk]}, format="json")
    assert res.data["removed"] == 1
    assert admin.patch(f"{API}/{gid}/", {"name": "NMR workshop 2026"}, format="json").data["name"] == "NMR workshop 2026"
    assert admin.delete(f"{API}/{gid}/").status_code == 204


def test_automatic_groups_are_protected(booked):
    admin = client_for(booked.admin)
    gid = booked.category_group.pk
    assert admin.post(f"{API}/{gid}/members/add/", {"user_ids": [booked.operator.pk]}, format="json").status_code == 409
    assert admin.delete(f"{API}/{gid}/").status_code == 409
    assert admin.patch(f"{API}/{gid}/", {"name": "Renamed"}, format="json").status_code == 400
    assert admin.patch(f"{API}/{gid}/", {"is_archived": True}, format="json").data["is_archived"] is True


def test_options_and_user_search(booked):
    admin = client_for(booked.admin)
    opts = admin.get(f"{API}/options/").data
    assert any(d["name"] == "Delhi University" and d["department_type"] == "external" for d in opts["departments"])
    assert {"value": "summary", "label": "One summary copy to CC / BCC"} in opts["cc_modes"]
    found = admin.get(f"{API}/users/search/", {"q": "guest"}).data["results"]
    assert [u["id"] for u in found] == [booked.external.pk]

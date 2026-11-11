"""M2: feature flag, access matrix, visibility, sanitiser and append-only audit."""

import pytest

from iic_booking.connect import access
from iic_booking.connect.models import CommunityMembership
from iic_booking.connect.models import ConnectRole
from iic_booking.connect.models import ConnectRoleAssignment
from iic_booking.connect.models import DiscussionCategory
from iic_booking.connect.models import ImmutableRecordError
from iic_booking.connect.models import MembershipAuditLog
from iic_booking.connect.models import MembershipStatus
from iic_booking.connect.models import ModerationAction
from iic_booking.connect.models import RecognitionBadge
from iic_booking.connect.models import Visibility
from iic_booking.connect.sanitize import clean_plain
from iic_booking.connect.sanitize import mentioned_user_ids
from iic_booking.connect.sanitize import referenced_attachment_ids
from iic_booking.connect.sanitize import sanitize_post_html
from iic_booking.connect.sanitize import strip_attachment_refs
from iic_booking.users.models.user_type import UserType

from .conftest import API
from .conftest import add_member
from .conftest import client_for
from .conftest import general_community
from .conftest import group_community
from .conftest import make_department
from .conftest import make_external
from .conftest import make_thread
from .conftest import make_user

pytestmark = pytest.mark.django_db


def fresh(user):
    access.invalidate(user)
    return user


# --- seed -------------------------------------------------------------------------------------------------------


def test_seed_creates_single_general_community_with_categories():
    general = general_community()
    slugs = set(DiscussionCategory.objects.filter(community=general).values_list("slug", flat=True))
    assert {
        "general-discussion", "announcements", "portal-support", "feature-requests", "suggestions-feedback",
        "research-collaboration", "training-experiences", "general-instrumentation",
    } <= slugs
    assert DiscussionCategory.objects.get(community=general, slug="announcements").staff_only_posting
    assert RecognitionBadge.objects.count() >= 5


# --- flag ---------------------------------------------------------------------------------------------------------


def test_flag_off_by_default(settings):
    assert settings.IITR_CONNECT_ENABLED is False


def test_bootstrap_reports_disabled_when_flag_off(settings):
    settings.IITR_CONNECT_ENABLED = False
    student = make_user()
    res = client_for(student).get(f"{API}/bootstrap/")
    assert res.status_code == 200
    assert res.json()["enabled"] is False
    assert res.json()["community_count"] == 0
    assert "no-store" in res["Cache-Control"]
    assert "noindex" in res["X-Robots-Tag"]


def test_unauthenticated_rejected(connect_on):
    res = client_for().get(f"{API}/bootstrap/")
    assert res.status_code in (401, 403)


def test_flag_off_grants_nothing(settings):
    settings.IITR_CONNECT_ENABLED = False
    student = make_user()
    assert access.accessible_community_ids(student) == frozenset()
    assert not access.visible_threads(student).exists()


def test_pilot_list_restricts(connect_on):
    listed = make_user(email="pilot.user@iitr.ac.in")
    other = make_user()
    connect_on.IITR_CONNECT_PILOT_EMAILS = "Pilot.User@iitr.ac.in"
    assert access.user_enabled(listed)
    assert not access.user_enabled(other)
    assert client_for(other).get(f"{API}/bootstrap/").json()["enabled"] is False


def test_inactive_user_has_no_access(connect_on):
    student = make_user(force_inactive=True)  # User.save() derives is_active from approval and force_inactive
    assert student.is_active is False
    assert access.accessible_community_ids(student) == frozenset()


# --- General IITR Discussions: internal only ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_type", [UserType.STUDENT, UserType.INDIVIDUAL_STUDENT, UserType.OPERATOR, UserType.MANAGER, UserType.DEPT_ADMIN]
)
def test_internal_users_read_and_post_general(connect_on, user_type):
    user = make_user(user_type)
    general = general_community()
    assert access.can_read_community(user, general.pk)
    assert access.can_post_in(user, general.pk)


def test_internal_faculty_reads_general(connect_on):
    faculty = make_user(UserType.FACULTY, department=make_department("Physics"))
    assert access.can_read_community(faculty, general_community().pk)


@pytest.mark.parametrize(
    "user_type",
    [UserType.INSTITUTE, UserType.EXTERNAL, UserType.RND, UserType.EXTERNAL_STARTUP_MSME,
     UserType.STARTUP_INCUBATED_IITR, UserType.OTHER, UserType.ORG_ADMIN],
)
def test_external_users_never_see_general(connect_on, user_type):
    external = make_user(user_type, email=f"x{user_type}@corp.example")
    general = general_community()
    make_thread(general, make_user(), title="Internal only thread")
    assert not access.can_read_community(external, general.pk)
    assert not access.visible_threads(external).exists()
    assert client_for(external).get(f"{API}/bootstrap/").json()["can_use_general"] is False


def test_faculty_of_external_department_is_external(connect_on):
    faculty = make_user(UserType.FACULTY, department=make_department("Other Univ", department_type="external"))
    assert not access.can_read_community(faculty, general_community().pk)


def test_revoked_internal_user_loses_general(connect_on):
    student = make_user()
    general = general_community()
    CommunityMembership.objects.create(community=general, user=student, status=MembershipStatus.REVOKED)
    assert not access.can_read_community(student, general.pk)


def test_suspended_internal_user_reads_but_cannot_post_general(connect_on):
    student = make_user()
    general = general_community()
    CommunityMembership.objects.create(community=general, user=student, status=MembershipStatus.SUSPENDED)
    assert access.can_read_community(student, general.pk)
    assert not access.can_post_in(student, general.pk)


# --- Group communities: members only, no cross-group access --------------------------------------------------------


def test_group_member_reads_own_group_only(connect_on):
    a, b = group_community(), group_community()
    user = make_user()
    add_member(a, user)
    make_thread(a, user, title="Group A thread")
    make_thread(b, make_user(), title="Group B secret thread")
    ids = access.accessible_community_ids(user)
    assert a.pk in ids and b.pk not in ids
    titles = set(access.visible_threads(user).values_list("title", flat=True))
    assert "Group B secret thread" not in titles
    assert "Group A thread" in titles


def test_external_booker_sees_only_their_group(connect_on):
    a, _other = group_community(), group_community()
    ext = make_external()
    add_member(a, ext)
    ids = access.accessible_community_ids(ext)
    assert ids == frozenset({a.pk})


@pytest.mark.parametrize("status", [MembershipStatus.LAPSED, MembershipStatus.REVOKED])
def test_lapsed_or_revoked_member_has_no_access(connect_on, status):
    a = group_community()
    user = make_user()
    add_member(a, user, status=status)
    assert not access.can_read_community(user, a.pk)
    assert not access.visible_threads(user).filter(community=a).exists()


def test_suspended_member_reads_but_cannot_post(connect_on):
    a = group_community()
    user = make_user()
    add_member(a, user, status=MembershipStatus.SUSPENDED)
    assert access.can_read_community(user, a.pk)
    assert not access.can_post_in(user, a.pk)


def test_archived_community_is_read_only(connect_on):
    a = group_community(status="archived")
    user = make_user()
    add_member(a, user)
    assert access.can_read_community(user, a.pk)
    assert not access.can_post_in(user, a.pk)


def test_test_only_community_hidden_from_real_users(connect_on):
    a = group_community(is_test_only=True)
    real = make_user()
    add_member(a, real)
    tester = make_user(is_test_account=True)
    add_member(a, tester)
    assert not access.can_read_community(real, a.pk)
    assert access.can_read_community(tester, a.pk)


# --- Admin roles --------------------------------------------------------------------------------------------------


def test_portal_admin_reads_and_moderates_everything(connect_on):
    a = group_community(is_test_only=True)
    admin = make_user(UserType.ADMIN)
    assert access.can_read_community(admin, a.pk)
    assert access.can_moderate(admin, a.pk)
    assert access.role_badge(admin, a.pk) == access.BADGE_PORTAL_ADMIN


def test_community_admin_role_is_assigned_not_self_claimed(connect_on):
    a = group_community()
    user = make_user()
    assert not access.can_moderate(user, a.pk)
    ConnectRoleAssignment.objects.create(user=user, role=ConnectRole.COMMUNITY_ADMIN)
    fresh(user)
    assert access.can_moderate(user, a.pk)
    assert access.snapshot(user).community_admin


def test_moderator_from_oic_source_and_override(connect_on):
    a = group_community()
    oic = make_user(UserType.MANAGER)
    add_member(a, oic, via_booking=False, via_oic=True)
    member = make_user()
    m = add_member(a, member)
    assert access.can_moderate(oic, a.pk)
    assert access.role_badge(oic, a.pk) == access.BADGE_OIC
    assert not access.can_moderate(member, a.pk)
    m.moderator_override = True
    m.save()
    fresh(member)
    assert access.can_moderate(member, a.pk)
    assert access.role_badge(member, a.pk) == access.BADGE_MODERATOR


def test_operator_badge(connect_on):
    a = group_community()
    op = make_user(UserType.OPERATOR)
    add_member(a, op, via_booking=False, via_operator=True)
    assert access.role_badge(op, a.pk) == access.BADGE_OPERATOR


# --- Visibility of hidden / removed content -------------------------------------------------------------------------


def test_hidden_thread_visible_to_moderator_and_author_only(connect_on):
    a = group_community()
    author, member, mod = make_user(), make_user(), make_user(UserType.MANAGER)
    for u in (author, member):
        add_member(a, u)
    add_member(a, mod, via_booking=False, via_oic=True)
    t = make_thread(a, author, visibility=Visibility.HIDDEN)
    assert access.visible_threads(author).filter(pk=t.pk).exists()
    assert access.visible_threads(mod).filter(pk=t.pk).exists()
    assert not access.visible_threads(member).filter(pk=t.pk).exists()


def test_removed_thread_not_visible_to_author(connect_on):
    a = group_community()
    author = make_user()
    add_member(a, author)
    t = make_thread(a, author, visibility=Visibility.REMOVED)
    assert not access.visible_threads(author).filter(pk=t.pk).exists()
    assert not access.visible_posts(author).filter(thread=t).exists()


def test_get_thread_or_404_for_other_group(connect_on):
    from django.http import Http404

    a, b = group_community(), group_community()
    user = make_user()
    add_member(a, user)
    t = make_thread(b, make_user())
    with pytest.raises(Http404):
        access.get_thread_or_404(user, t.pk)


# --- Sanitiser ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        '<script>alert(1)</script><p>ok</p>',
        '<img src=x onerror=alert(1)>',
        '<a href="javascript:alert(1)">x</a>',
        '<p onclick="alert(1)">x</p>',
        '<iframe src="https://evil.example"></iframe>',
        '<svg><script>alert(1)</script></svg>',
        '<a href="/relative">x</a>',
        '<img src="https://tracker.example/p.gif">',
        '<p style="background:url(javascript:alert(1))">x</p>',
    ],
)
def test_sanitiser_blocks_xss(payload):
    out = sanitize_post_html(payload)
    lowered = out.lower()
    for bad in ("<script", "onerror", "javascript:", "onclick", "<iframe", "<svg", "src=", "style=", 'href="/relative"'):
        assert bad not in lowered


def test_sanitiser_keeps_safe_formatting():
    html = (
        '<p><strong>Bold</strong> <a href="https://iitr.ac.in">link</a></p>'
        '<pre><code class="language-python">print(1)</code></pre>'
        '<blockquote><p>quote</p></blockquote>'
        '<span data-mention-id="42">@Asha</span>'
        '<img data-attachment-id="0f8fad5b-d9cb-469f-a165-70867728950e" alt="spectrum">'
    )
    out = sanitize_post_html(html)
    assert "<strong>Bold</strong>" in out
    assert 'rel="noopener noreferrer nofollow ugc"' in out
    assert 'class="language-python"' in out
    assert "<blockquote>" in out
    assert mentioned_user_ids(out) == [42]
    assert referenced_attachment_ids(out) == ["0f8fad5b-d9cb-469f-a165-70867728950e"]
    assert "data-attachment-id" not in strip_attachment_refs(out, set())


def test_plain_text_is_escaped_and_wrapped():
    out = sanitize_post_html("line one\nline <two>\n\nsecond para")
    assert out.startswith("<p>") and "&lt;two&gt;" in out and out.count("<p>") == 2


def test_clean_plain_title():
    assert clean_plain("  <b>Hello</b>\n\tworld\x00 ", max_length=200) == "Hello world"


# --- Append-only audit ------------------------------------------------------------------------------------------------


def test_audit_rows_are_append_only(connect_on):
    a = group_community()
    user = make_user()
    row = MembershipAuditLog.objects.create(community=a, user=user, action="joined")
    with pytest.raises(ImmutableRecordError):
        row.save()
    with pytest.raises(ImmutableRecordError):
        row.delete()
    with pytest.raises(ImmutableRecordError):
        MembershipAuditLog.objects.filter(pk=row.pk).update(action="revoked")
    act = ModerationAction.objects.create(actor=user, community=a, action="lock", target_type="thread", target_id="1")
    with pytest.raises(ImmutableRecordError):
        act.delete()

"""Organisations an external user adds themself (9 Nov 2026).

When their organisation is not in the list, an external user completing their profile adds it. The organisation is
created at once and the user is put in it, so they can go on to book. It is marked *unverified*: an
``OrganizationRequest`` stays pending with ``created_department`` set, and the admin later, on External Department
Addition / Verification:

* **approves** it, optionally correcting the name (the department is renamed in place), or
* **merges** it into an organisation already listed (a duplicate): everything that pointed at the added one, users
  included, is moved to the listed one and the added one is deleted, or
* **rejects** it: its users are taken out of it and asked for their organisation again on their next visit.
"""

from __future__ import annotations

from django.db import IntegrityError
from django.db import transaction


class OrganisationError(Exception):
    pass


def _clean_name(name: str) -> str:
    return " ".join(str(name or "").split())[:255]


def add_for_user(user, name: str, state: str, subcategory: str):
    """Put ``user`` in the organisation ``name`` (State/UT ``state``), creating it unverified if it is not listed.

    An organisation already listed under the same name and State/UT (any case) is used as it is. Returns the
    department.
    """
    from iic_booking.users.models import Department
    from iic_booking.users.models import OrganizationRequest
    from iic_booking.users.models.department import DepartmentType
    from iic_booking.users.models.department import ExternalDepartmentSubcategory

    name = _clean_name(name)
    with transaction.atomic():
        dept = Department.objects.filter(
            department_type=DepartmentType.EXTERNAL, name__iexact=name, state=state,
        ).order_by("pk").first()
        if dept is None and Department.objects.filter(name__iexact=name).exists():
            # Names are unique portal-wide: the same name in another State/UT gets the State/UT added.
            name = _clean_name(f"{name} ({state.replace('_', ' ').title()})")
            dept = Department.objects.filter(department_type=DepartmentType.EXTERNAL, name__iexact=name).first()
            if dept is None and Department.objects.filter(name__iexact=name).exists():
                raise OrganisationError("That name is already used. Add your organisation's full name.")
        if dept is None:
            dept = Department.objects.create(
                name=name,
                department_type=DepartmentType.EXTERNAL,
                external_subcategory=subcategory or ExternalDepartmentSubcategory.GOVT_RND,
                state=state,
            )
            OrganizationRequest.objects.create(
                name=name, state=state, external_subcategory=subcategory or ExternalDepartmentSubcategory.GOVT_RND,
                email=user.email, requester_name=(user.name or "")[:255], created_department=dept,
                notes="Added by the user while completing their profile; to be verified (correct the name, or merge "
                      "into a listed organisation if it is a duplicate).",
            )
        user.department = dept
        user.organization_request = None
        user.save(update_fields=["department", "organization_request"])
    return dept


def unverified_department_ids() -> set[int]:
    """Departments added by users and not yet checked by the admin."""
    from iic_booking.users.models import OrganizationRequest

    return set(
        OrganizationRequest.objects.filter(
            status=OrganizationRequest.Status.PENDING, created_department__isnull=False,
        ).values_list("created_department_id", flat=True)
    )


def approve(req, final_name: str, by) -> None:
    """Accept a user-added organisation, renaming it to ``final_name`` if the admin corrected it."""
    from iic_booking.users.models import Department
    from iic_booking.users.models import OrganizationRequest

    dept = req.created_department
    final_name = _clean_name(final_name) or dept.name
    if Department.objects.filter(name__iexact=final_name).exclude(pk=dept.pk).exists():
        raise OrganisationError(
            f"'{final_name}' is already listed. Merge this organisation into it instead.")
    with transaction.atomic():
        if dept.name != final_name:
            dept.name = final_name
            dept.save(update_fields=["name"])
        req.approved_name = final_name
        req.status = OrganizationRequest.Status.APPROVED
        req.approved_by = by
        req.save(update_fields=["approved_name", "status", "approved_by"])


def _repoint(source, target) -> None:
    """Move every row that points at ``source`` to ``target``."""
    for rel in source._meta.related_objects:
        if rel.many_to_many:
            accessor = rel.get_accessor_name()
            for obj in getattr(source, accessor).all():
                getattr(obj, rel.field.name).add(target)
            continue
        if rel.field.model._meta.proxy or not rel.field.concrete:
            continue
        rel.related_model._base_manager.filter(**{rel.field.name: source}).update(**{rel.field.name: target})
    for field in source._meta.many_to_many:
        getattr(target, field.name).add(*getattr(source, field.name).all())


def merge(req, target, by) -> None:
    """The user-added organisation is a duplicate of ``target``: move everything to ``target`` and delete it."""
    from iic_booking.users.models import OrganizationRequest
    from iic_booking.users.models.department import DepartmentType

    source = req.created_department
    if target.pk == source.pk:
        raise OrganisationError("Choose a different organisation to merge into.")
    if target.department_type != DepartmentType.EXTERNAL:
        raise OrganisationError("Merge only into an external organisation.")
    try:
        with transaction.atomic():
            _repoint(source, target)
            req.refresh_from_db()
            req.created_department = target
            req.approved_name = target.name
            req.status = OrganizationRequest.Status.APPROVED
            req.approved_by = by
            req.notes = (req.notes + f"\nMerged into '{target.name}' (id {target.pk}).").strip()
            req.save(update_fields=["created_department", "approved_name", "status", "approved_by", "notes"])
            source.delete()
    except IntegrityError:
        raise OrganisationError(
            "These two organisations both have records that can exist only once (for example a wallet setting). "
            "Nothing was changed; merge them from the Django admin.")


def reject(req, by) -> None:
    """Not a real organisation: take its users out of it and remove it if nothing else uses it."""
    from django.db.models.deletion import ProtectedError

    from iic_booking.users.models import OrganizationRequest

    dept = req.created_department
    with transaction.atomic():
        dept.users.update(department=None)
        req.status = OrganizationRequest.Status.REJECTED
        req.approved_by = by
        req.created_department = None
        req.save(update_fields=["status", "approved_by", "created_department"])
        if not _in_use(dept):
            try:
                dept.delete()
            except ProtectedError:
                pass


def _in_use(dept) -> bool:
    for rel in dept._meta.related_objects:
        if rel.many_to_many or not rel.field.concrete:
            continue
        if rel.related_model._base_manager.filter(**{rel.field.name: dept}).exists():
            return True
    return False

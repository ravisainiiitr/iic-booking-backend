# Tinkering Lab staff assignment (piped into `manage.py shell` by the "Assign Tinkering Lab Staff" workflow).
# Every non-test equipment of the Tinkering Lab gets exactly one Officer in Charge and one Lab Operator:
# 3D printer category -> 3D printer operator, everything else -> general operator. Equipment of any other
# department (IIC etc.) is never written. Inactive operator accounts are activated with the admin-approval flags
# (no approval email). APPLY=1 writes everything in one transaction; otherwise a dry run prints the plan only.
import os

from django.db import transaction
from django.db.models import Q

from iic_booking.equipment.models import Equipment, EquipmentManager, EquipmentOperator
from iic_booking.equipment.testdata import exclude_test_equipment
from iic_booking.users.models import Department, DepartmentType, User, UserType

APPLY = os.environ.get("APPLY") == "1"
OIC_EMAIL = "ysingh.mme@iitr.ac.in"
PRINTER_OPERATOR_EMAIL = "jobwork.tl@iitr.ac.in"
OTHER_OPERATOR_EMAIL = "amit.mtl@iitr.ac.in"
CONTACT_FIELDS = ("honorific", "disable_booking_confirmation_email", "office_address", "alternate_phone_number")


def is_3d_printer(equipment):
    cat = equipment.category
    text = f"{getattr(cat, 'name', '')} {getattr(cat, 'code', '')}".lower().replace(" ", "")
    return "3dprint" in text


def describe_user(u):
    dept = u.department
    return (
        f"id={u.pk} {u.name or ''} <{u.email}> type={u.user_type} active={u.is_active} "
        f"email_verified={u.email_verified} admin_approved={u.admin_approved} force_inactive={u.force_inactive} "
        f"access_on_hold={getattr(u, 'access_on_hold', None)} last_login={u.last_login} "
        f"test={getattr(u, 'is_test_account', False)} dept={getattr(dept, 'name', None)} "
        f"dept_type={getattr(dept, 'department_type', None)}"
    )


def activate(email):
    """Same flags as an admin approval (no approval email). Returns True when the account changed."""
    u = User.objects.filter(email__iexact=email).first()
    if u is None or u.is_active:
        return False
    fields = ["email_verified", "admin_approved", "force_inactive"]
    u.email_verified = True
    u.admin_approved = True
    u.force_inactive = False
    if getattr(u, "access_on_hold", False):
        u.access_on_hold = False
        fields.append("access_on_hold")
    u.save(update_fields=fields)
    u.refresh_from_db()
    print(f"  activated {email}: active={u.is_active}")
    return True


def find_user(email, required_type):
    u = User.objects.filter(email__iexact=email).select_related("department").first()
    if u is None:
        print(f"  {email}: NOT FOUND")
        return None, f"{email} has no account"
    print(f"  {email}: {describe_user(u)}")
    problems = []
    if u.user_type != required_type:
        problems.append(f"account type is {u.user_type}, needs {required_type}")
    if not u.is_active:
        problems.append("account is inactive")
    if getattr(u.department, "department_type", None) != DepartmentType.INTERNAL:
        problems.append("department is not internal")
    return u, (f"{email}: " + "; ".join(problems)) if problems else None


def same_contact(previous):
    return {f: getattr(previous, f) for f in CONTACT_FIELDS} if previous else {}


print(f"mode: {'APPLY' if APPLY else 'DRY RUN'}")
depts = list(Department.objects.filter(Q(name__icontains="tinkering") | Q(code__iexact="TL")))
print("tinkering departments:", [(d.pk, d.code, d.name, d.department_type) for d in depts])
if len(depts) != 1:
    raise SystemExit("expected exactly one Tinkering Lab department; nothing changed")
lab = depts[0]

class Blocked(Exception):
    pass


def run():
    print("accounts:")
    to_activate = []
    for email in (PRINTER_OPERATOR_EMAIL, OTHER_OPERATOR_EMAIL):
        u = User.objects.filter(email__iexact=email).first()
        if u is not None and not u.is_active:
            print(f"  {email}: inactive, {'activating' if APPLY else 'would be activated on apply'}")
            print(f"    before: {describe_user(u)}")
            to_activate.append(email)
            if APPLY:
                activate(email)
    oic, p1 = find_user(OIC_EMAIL, UserType.MANAGER)
    printer_op, p2 = find_user(PRINTER_OPERATOR_EMAIL, UserType.OPERATOR)
    other_op, p3 = find_user(OTHER_OPERATOR_EMAIL, UserType.OPERATOR)
    problems = [p for p in (p1, p2, p3) if p]
    if not APPLY:
        problems = [p for p in problems if not (p.split(":")[0] in to_activate and p.endswith("account is inactive"))]

    all_lab = Equipment.objects.filter(internal_department=lab).select_related("category")
    lab_equipment = list(exclude_test_equipment(all_lab).order_by("category__name", "code"))
    print(f"equipment: {len(lab_equipment)} non-test (skipping {all_lab.count() - len(lab_equipment)} test)")

    plan = []
    for eq in lab_equipment:
        managers = list(EquipmentManager.objects.filter(equipment=eq).select_related("manager"))
        operators = list(EquipmentOperator.objects.filter(equipment=eq).select_related("operator"))
        printer = is_3d_printer(eq)
        print(
            f"- {eq.code} | {eq.name} | category={getattr(eq.category, 'name', None)} | status={eq.status} | "
            f"{'3D printer' if printer else 'other'}\n"
            f"    OIC now: {[m.manager.email for m in managers]} -> {OIC_EMAIL}\n"
            f"    operators now: {[(o.operator.email, o.role) for o in operators]} -> "
            f"{PRINTER_OPERATOR_EMAIL if printer else OTHER_OPERATOR_EMAIL}"
        )
        plan.append((eq, managers, operators, printer_op if printer else other_op))

    if problems:
        print("BLOCKED:")
        for p in problems:
            print("  -", p)
        raise Blocked("accounts not eligible; nothing changed")
    if not APPLY:
        print("dry run: nothing changed")
        return

    changed = 0
    for eq, managers, operators, target_op in plan:
        if [m.manager_id for m in managers] != [oic.pk]:
            prev = next((m for m in managers if m.manager_id == oic.pk), None)
            EquipmentManager.objects.filter(equipment=eq).delete()
            EquipmentManager.objects.create(equipment=eq, manager=oic, **same_contact(prev))
            changed += 1
        if [(o.operator_id, o.role) for o in operators] != [(target_op.pk, EquipmentOperator.Role.PRIMARY)]:
            prev = next((o for o in operators if o.operator_id == target_op.pk), None)
            EquipmentOperator.objects.filter(equipment=eq).delete()
            EquipmentOperator.objects.create(
                equipment=eq, operator=target_op, role=EquipmentOperator.Role.PRIMARY, **same_contact(prev)
            )
            changed += 1
    print(f"applied: {changed} assignment lists replaced on {len(plan)} equipment")


try:
    with transaction.atomic():
        run()
except Blocked as exc:
    raise SystemExit(str(exc))

"""Show, plan, seed or change the per-department module switches.

    manage.py department_modules                      # current matrix (default, same as --show)
    manage.py department_modules --plan               # starting matrix the seeding rules give for today's data
    manage.py department_modules --seed               # create missing rows from --plan (idempotent; departments
                                                      # created after installation stay off)
    manage.py department_modules --set IIC remote_analysis --off --reason "..." --actor admin@iitr.ac.in
    manage.py department_modules --set TL training --on --test-only on --reason "..." --actor admin@iitr.ac.in
    manage.py department_modules --history [--department IIC] [--module dsa]

``--json`` prints machine-readable output. ``--all`` also lists departments where every module is off.
"""

from __future__ import annotations

import json

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from iic_booking.department_modules import seeding, services
from iic_booking.department_modules.constants import ALL_MODULES
from iic_booking.department_modules.errors import DepartmentModuleError

SHORT = {"dsa": "DSA", "remote_analysis": "RAA", "training": "TRAINING", "procurement": "PROCUREMENT"}


def _cell_text(cell: dict) -> str:
    if not cell.get("configured") and cell.get("source") != "procurement":
        return "default(on)" if cell["enabled"] else "off(new)"
    if not cell["enabled"]:
        return "off"
    return "test-only" if cell["test_users_only"] else "on"


def _bool_arg(value: str | None) -> bool | None:
    if value is None:
        return None
    text = value.strip().lower()
    if text in {"on", "yes", "true", "1"}:
        return True
    if text in {"off", "no", "false", "0"}:
        return False
    raise CommandError("--test-only must be on or off.")


class Command(BaseCommand):
    help = "Show, plan, seed or change the per-department module switches (DSA, RAA, Training, Procurement)."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--show", action="store_true", help="Print the current matrix (default).")
        mode.add_argument("--plan", action="store_true", help="Print the starting matrix the seeding rules produce.")
        mode.add_argument("--seed", action="store_true", help="Create missing rows from the plan (idempotent).")
        mode.add_argument("--set", nargs=2, metavar=("DEPARTMENT", "MODULE"), help="Change one cell.")
        mode.add_argument("--history", action="store_true", help="Print the audit history.")
        state = parser.add_mutually_exclusive_group()
        state.add_argument("--on", action="store_true")
        state.add_argument("--off", action="store_true")
        parser.add_argument("--test-only", dest="test_only", choices=["on", "off"], default=None)
        parser.add_argument("--reason", default="")
        parser.add_argument("--actor", default="", help="Email of the Main Administrator making the change.")
        parser.add_argument("--department", default=None)
        parser.add_argument("--module", default=None, choices=[m.value for m in ALL_MODULES])
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--all", action="store_true", help="Include departments where every module is off.")

    def handle(self, *args, **opts):
        try:
            if opts["plan"]:
                return self._plan(opts)
            if opts["seed"]:
                return self._seed(opts)
            if opts["set"]:
                return self._set(opts)
            if opts["history"]:
                return self._history(opts)
            return self._show(opts)
        except DepartmentModuleError as exc:
            raise CommandError(exc.message) from exc

    # -- modes -------------------------------------------------------------
    def _show(self, opts):
        data = services.matrix()
        if opts["json"]:
            self.stdout.write(json.dumps(data, default=str, indent=2))
            return
        rows = []
        for d in data["departments"]:
            texts = {k: _cell_text(d["cells"][k]) for k in SHORT}
            if not opts["all"] and all(t in {"off", "off(new)"} for t in texts.values()):
                continue
            rows.append((d["code"] or d["name"], d["id"], texts))
        self._table(rows, hidden=len(data["departments"]) - len(rows))

    def _plan(self, opts):
        facts, matrix = seeding.plan(apps.get_model)
        existing = set(
            apps.get_model("department_modules", "DepartmentModuleSetting").objects.values_list("department_id", "module_key")
        )
        if opts["json"]:
            out = [
                {
                    "department_id": f.id,
                    "code": f.code,
                    "name": f.name,
                    "modules": {
                        k: {"enabled": s.enabled, "test_users_only": s.test_users_only, "note": s.note,
                            "row_exists": (f.id, k) in existing}
                        for k, s in matrix[f.id].items()
                    },
                }
                for f in facts
            ]
            self.stdout.write(json.dumps(out, indent=2))
            return
        rows = []
        notes = []
        for f in facts:
            seeds = matrix[f.id]
            texts = {k: ("off" if not s.enabled else "test-only" if s.test_users_only else "on") for k, s in seeds.items()}
            texts["procurement"] = "(own config)"
            if not opts["all"] and all(not s.enabled for s in seeds.values()):
                continue
            rows.append((f.code or f.name, f.id, texts))
            notes.extend(f"  {f.code or f.name} {SHORT[k]}: {s.note}" for k, s in seeds.items() if s.enabled)
        self._table(rows, hidden=len(facts) - len(rows))
        if notes:
            self.stdout.write("Why ON:")
            for line in notes:
                self.stdout.write(line)
        self.stdout.write(f"Rows already present (left unchanged by --seed): {len(existing)}")

    def _seed(self, opts):
        created = seeding.seed(apps.get_model)
        self.stdout.write(f"Created {len(created)} rows (existing rows are never changed).")
        for dept_id, key, s in created:
            if s.enabled:
                self.stdout.write(f"  dept {dept_id} {SHORT[key]}: {'test-only' if s.test_users_only else 'on'} ({s.note})")
        self._show(opts)

    def _set(self, opts):
        from iic_booking.users.models import User

        dept_raw, module = opts["set"]
        if module not in [m.value for m in ALL_MODULES]:
            raise CommandError(f"Unknown module {module!r}; use one of {', '.join(m.value for m in ALL_MODULES)}.")
        enabled = True if opts["on"] else False if opts["off"] else None
        actor = User.objects.filter(email__iexact=(opts["actor"] or "").strip(), is_active=True).first()
        if actor is None:
            raise CommandError("--actor must be the email of an active Main Administrator.")
        department = services.get_department(dept_raw)
        cell = services.set_module(
            actor,
            department,
            module,
            enabled=enabled,
            test_users_only=_bool_arg(opts["test_only"]),
            reason=opts["reason"],
        )
        self.stdout.write(f"{department.code or department.name} {SHORT[module]} -> {_cell_text(cell)}")

    def _history(self, opts):
        dept_id = services.get_department(opts["department"]).pk if opts["department"] else None
        entries = services.history(department_id=dept_id, module_key=opts["module"])
        if opts["json"]:
            self.stdout.write(json.dumps(entries, default=str, indent=2))
            return
        for e in entries:
            self.stdout.write(
                f"{e['created_at']}  {e['department']:<12} {SHORT.get(e['module_key'], e['module_key']):<11} "
                f"{e['action']:<28} by {e['actor'] or 'system'}: {e['reason']}"
            )

    def _table(self, rows, *, hidden: int):
        header = f"{'DEPARTMENT':<14}{'ID':>5}  " + "".join(f"{SHORT[k]:<14}" for k in SHORT)
        self.stdout.write(header)
        for label, dept_id, texts in rows:
            self.stdout.write(f"{label[:13]:<14}{dept_id:>5}  " + "".join(f"{texts[k]:<14}" for k in SHORT))
        if hidden:
            self.stdout.write(f"(+{hidden} departments with every module off; --all lists them)")

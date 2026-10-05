import pytest


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Run the suite against a portal where the department module switches are not installed.

    The seeding migration gives every department in the freshly migrated test database (e.g. "General") an explicit
    row and records the installation, after which every department a test creates would start with DSA, Remote
    Analysis and Training off. Tests of other modules are not about those switches, so they get the pre-switch
    behaviour; ``iic_booking/department_modules/tests`` install the switches explicitly (``installed`` fixture) to
    test the rules, including the new-department default.
    """
    from django.db import connection

    from iic_booking.department_modules.models import (
        DepartmentModuleAuditLog,
        DepartmentModuleSetting,
        DepartmentModulesInstallation,
    )

    with django_db_blocker.unblock(), connection.cursor() as cursor:
        for model in (DepartmentModuleAuditLog, DepartmentModuleSetting, DepartmentModulesInstallation):
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)}")

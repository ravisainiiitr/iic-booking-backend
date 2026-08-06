import django

django.setup()

from types import SimpleNamespace

from django.db import connection

from iic_booking.device_provisioning import services
from iic_booking.device_provisioning.models import (
    DeviceAssignment,
    DeviceAuditLog,
    DeviceBootstrapToken,
    DeviceCertificate,
    DeviceHeartbeat,
    DeviceInventory,
    DevicePolicy,
    ProvisionedDevice,
    ProvisioningSession,
)

with connection.cursor() as c:
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("CREATE TABLE IF NOT EXISTS users_user (id integer PRIMARY KEY)")
    c.execute(
        "CREATE TABLE IF NOT EXISTS users_department (id integer PRIMARY KEY, name varchar(255))"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS equipment_equipment (id integer PRIMARY KEY, name varchar(255))"
    )

# Create models without constraint checking (sqlite stub deps)
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

models_to_create = [
    ProvisionedDevice,
    DevicePolicy,
    DeviceInventory,
    DeviceHeartbeat,
    DeviceCertificate,
    DeviceAssignment,
    ProvisioningSession,
    DeviceBootstrapToken,
    DeviceAuditLog,
]
se = connection.schema_editor(atomic=False)
se.__enter__()
try:
    se.deferred_sql = []  # avoid FK checks on exit
    for Model in models_to_create:
        try:
            se.create_model(Model)
            print("created", Model.__name__)
        except Exception as exc:  # noqa: BLE001
            print("skip", Model.__name__, type(exc).__name__, exc)
finally:
    # bypass check_constraints
    connection.connection.execute("PRAGMA foreign_keys=OFF")
    BaseDatabaseSchemaEditor.__exit__(se, None, None, None)

with connection.cursor() as c:
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("INSERT OR IGNORE INTO users_user(id) VALUES (1)")

session, proof = services.create_session(
    payload={
        "device_type": "dsa",
        "machine_guid": "G1",
        "hostname": "H1",
        "mac_addresses": ["aa:bb:cc:dd:ee:ff"],
        "application_version": "1.0.0",
    }
)
print("session", session.id, session.status, bool(proof))

actor = SimpleNamespace(pk=1, id=1, is_authenticated=True)
session2, device, bootstrap = services.approve_session(
    session=session, actor=actor, display_name="Lab DSA"
)
print("approved", device.id, device.lifecycle, bool(bootstrap))

pack = services.claim_session(session=session2, session_proof=proof)
print("claimed", pack["device_uuid"], bool(pack["access_token"]), pack["device_type"])
device.refresh_from_db()
assert device.lifecycle == "active"
print("SMOKE_PASS")

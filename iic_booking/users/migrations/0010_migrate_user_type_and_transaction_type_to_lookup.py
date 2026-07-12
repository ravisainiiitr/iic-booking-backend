# Generated manually - Data migration to convert CharField to ForeignKey

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def create_lookup_values(apps, schema_editor):
    """Create MasterLookup entries for user types and transaction types."""
    MasterLookup = apps.get_model("users", "MasterLookup")
    
    # User types - all user types from UserType constants
    user_types = [
        ("admin", "Admin", 1),
        ("student", "Student", 2),
        ("faculty", "Faculty", 3),
        ("external", "Educational Institute", 4),
        ("manager", "Manager", 5),
        ("operator", "Operator", 6),
        ("finance", "Finance", 7),
        ("RND", "Govt R&D Center", 8),
        ("Industry", "Industry", 9),
    ]
    
    for code, name, order in user_types:
        MasterLookup.objects.get_or_create(
            category="user_type",
            code=code,
            defaults={
                "name": name,
                "display_order": order,
                "is_active": True,
            }
        )
    
    # Transaction types
    transaction_types = [
        ("credit", "Credit", 1),
        ("debit", "Debit", 2),
    ]
    
    for code, name, order in transaction_types:
        MasterLookup.objects.get_or_create(
            category="transaction_type",
            code=code,
            defaults={
                "name": name,
                "display_order": order,
                "is_active": True,
            }
        )


def migrate_user_type_data(apps, schema_editor):
    """Migrate user_type from CharField to ForeignKey."""
    User = apps.get_model("users", "User")
    MasterLookup = apps.get_model("users", "MasterLookup")
    
    # Get all user type lookups
    user_type_lookups = {
        lookup.code: lookup
        for lookup in MasterLookup.objects.filter(category="user_type")
    }
    
    # Get default user type (external)
    default_lookup = user_type_lookups.get("external")
    
    # Migrate each user
    for user in User.objects.all():
        # Check if user_type is a string (CharField) or already a ForeignKey
        user_type_value = getattr(user, "user_type", None)
        lookup = None
        
        if user_type_value:
            # If it's a string, convert it
            if isinstance(user_type_value, str):
                lookup = user_type_lookups.get(user_type_value)
            # If it's already a ForeignKey object, copy it
            elif hasattr(user_type_value, "id"):
                lookup = user_type_value
        
        # Use default if no lookup found
        if not lookup and default_lookup:
            lookup = default_lookup
        
        if lookup:
            user.user_type_fk = lookup
            user.save(update_fields=["user_type_fk"])


def migrate_transaction_type_data(apps, schema_editor):
    """Migrate transaction_type from CharField to ForeignKey."""
    WalletTransaction = apps.get_model("users", "WalletTransaction")
    MasterLookup = apps.get_model("users", "MasterLookup")
    
    # Get all transaction type lookups
    transaction_type_lookups = {
        lookup.code: lookup
        for lookup in MasterLookup.objects.filter(category="transaction_type")
    }
    
    # Migrate each transaction
    for transaction in WalletTransaction.objects.all():
        # Check if transaction_type is a string (CharField) or already a ForeignKey
        transaction_type_value = getattr(transaction, "transaction_type", None)
        if transaction_type_value:
            # If it's a string, convert it
            if isinstance(transaction_type_value, str):
                lookup = transaction_type_lookups.get(transaction_type_value)
                if lookup:
                    transaction.transaction_type_fk = lookup
                    transaction.save(update_fields=["transaction_type_fk"])
            # If it's already a ForeignKey object, copy it
            elif hasattr(transaction_type_value, "id"):
                transaction.transaction_type_fk = transaction_type_value
                transaction.save(update_fields=["transaction_type_fk"])


def reverse_migrate_user_type_data(apps, schema_editor):
    """Reverse migration - convert ForeignKey back to CharField."""
    User = apps.get_model("users", "User")
    
    for user in User.objects.all():
        if user.user_type_fk:  # ForeignKey field
            user.user_type = user.user_type_fk.code
            user.save(update_fields=["user_type"])


def reverse_migrate_transaction_type_data(apps, schema_editor):
    """Reverse migration - convert ForeignKey back to CharField."""
    WalletTransaction = apps.get_model("users", "WalletTransaction")
    
    for transaction in WalletTransaction.objects.all():
        if transaction.transaction_type_fk:  # ForeignKey field
            transaction.transaction_type = transaction.transaction_type_fk.code
            transaction.save(update_fields=["transaction_type"])


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0009_alter_masterlookup_options_and_more"),
    ]

    operations = [
        # Step 1: Create lookup values
        migrations.RunPython(create_lookup_values, migrations.RunPython.noop),
        
        # Step 2: Add temporary ForeignKey fields (nullable for migration)
        migrations.AddField(
            model_name="user",
            name="user_type_fk",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="users_temp",
                to="users.masterlookup",
                verbose_name="User Type (FK)",
            ),
        ),
        migrations.AddField(
            model_name="wallettransaction",
            name="transaction_type_fk",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="wallet_transactions_temp",
                to="users.masterlookup",
                verbose_name="Transaction Type (FK)",
            ),
        ),
        
        # Step 3: Migrate data
        migrations.RunPython(migrate_user_type_data, reverse_migrate_user_type_data),
        migrations.RunPython(migrate_transaction_type_data, reverse_migrate_transaction_type_data),
        
        # Step 4: Remove old CharField fields
        migrations.RemoveField(
            model_name="user",
            name="user_type",
        ),
        migrations.RemoveField(
            model_name="wallettransaction",
            name="transaction_type",
        ),
        
        # Step 5: Rename temporary fields to final names
        migrations.RenameField(
            model_name="user",
            old_name="user_type_fk",
            new_name="user_type",
        ),
        migrations.RenameField(
            model_name="wallettransaction",
            old_name="transaction_type_fk",
            new_name="transaction_type",
        ),
        
        # Step 6: Update field properties (keep nullable for now, can be made required later)
        migrations.AlterField(
            model_name="user",
            name="user_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                help_text="Type of user in the system",
                limit_choices_to={"category": "user_type", "is_active": True},
                on_delete=django.db.models.deletion.PROTECT,
                related_name="users",
                to="users.masterlookup",
                verbose_name="User Type",
            ),
        ),
        migrations.AlterField(
            model_name="wallettransaction",
            name="transaction_type",
            field=models.ForeignKey(
                help_text="Type of transaction",
                limit_choices_to={"category": "transaction_type", "is_active": True},
                on_delete=django.db.models.deletion.PROTECT,
                related_name="wallet_transactions",
                to="users.masterlookup",
                verbose_name="Transaction Type",
            ),
        ),
        # Step 7: Update supervisor limit_choices_to to use ForeignKey path
        migrations.AlterField(
            model_name="supervisor",
            name="faculty",
            field=models.ForeignKey(
                help_text="Faculty member supervising this student",
                limit_choices_to={"user_type__code": "faculty"},
                on_delete=django.db.models.deletion.PROTECT,
                related_name="supervised_students",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Faculty Supervisor",
            ),
        ),
        migrations.AlterField(
            model_name="supervisor",
            name="student",
            field=models.OneToOneField(
                help_text="Student who has this supervisor",
                limit_choices_to={"user_type__code": "student"},
                on_delete=django.db.models.deletion.CASCADE,
                related_name="supervisor_as_student",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Student",
            ),
        ),
    ]


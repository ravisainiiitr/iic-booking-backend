from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0054_external_billing_profile"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrganizationRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255, verbose_name="Requested organization name")),
                (
                    "state",
                    models.CharField(
                        max_length=50,
                        verbose_name="State / Union Territory",
                        choices=[
                            ("andhra_pradesh", "Andhra Pradesh"),
                            ("arunachal_pradesh", "Arunachal Pradesh"),
                            ("assam", "Assam"),
                            ("bihar", "Bihar"),
                            ("chhattisgarh", "Chhattisgarh"),
                            ("goa", "Goa"),
                            ("gujarat", "Gujarat"),
                            ("haryana", "Haryana"),
                            ("himachal_pradesh", "Himachal Pradesh"),
                            ("jharkhand", "Jharkhand"),
                            ("karnataka", "Karnataka"),
                            ("kerala", "Kerala"),
                            ("madhya_pradesh", "Madhya Pradesh"),
                            ("maharashtra", "Maharashtra"),
                            ("manipur", "Manipur"),
                            ("meghalaya", "Meghalaya"),
                            ("mizoram", "Mizoram"),
                            ("nagaland", "Nagaland"),
                            ("odisha", "Odisha"),
                            ("punjab", "Punjab"),
                            ("rajasthan", "Rajasthan"),
                            ("sikkim", "Sikkim"),
                            ("tamil_nadu", "Tamil Nadu"),
                            ("telangana", "Telangana"),
                            ("tripura", "Tripura"),
                            ("uttar_pradesh", "Uttar Pradesh"),
                            ("uttarakhand", "Uttarakhand"),
                            ("west_bengal", "West Bengal"),
                            ("andaman_nicobar", "Andaman and Nicobar Islands"),
                            ("chandigarh", "Chandigarh"),
                            ("dadra_nagar_haveli_daman_diu", "Dadra and Nagar Haveli and Daman and Diu"),
                            ("delhi", "Delhi"),
                            ("jammu_kashmir", "Jammu and Kashmir"),
                            ("ladakh", "Ladakh"),
                            ("lakshadweep", "Lakshadweep"),
                            ("puducherry", "Puducherry"),
                        ],
                    ),
                ),
                (
                    "external_subcategory",
                    models.CharField(
                        max_length=50,
                        default="govt_rnd",
                        verbose_name="External subcategory",
                        help_text="External subcategory (e.g. Educational Institute, Govt R&D Organizations, Industries).",
                        choices=[
                            ("educational_institute", "Educational Institute"),
                            ("govt_rnd", "Govt R&D Organizations"),
                            ("industries", "Industries"),
                        ],
                    ),
                ),
                (
                    "email",
                    models.EmailField(
                        blank=True,
                        max_length=254,
                        null=True,
                        verbose_name="Requester email",
                        help_text="Email of the user requesting this organization.",
                    ),
                ),
                ("notes", models.TextField(blank=True, verbose_name="Additional details")),
                (
                    "status",
                    models.CharField(
                        max_length=20,
                        choices=[("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")],
                        default="pending",
                        verbose_name="Status",
                    ),
                ),
                (
                    "approved_name",
                    models.CharField(
                        blank=True,
                        max_length=255,
                        verbose_name="Approved organization name",
                        help_text="Admin-edited final name used to create the Department.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_department",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="organization_requests",
                        to="users.department",
                    ),
                ),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="approved_organization_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Organization request",
                "verbose_name_plural": "Organization requests",
                "ordering": ["-created_at"],
            },
        ),
    ]

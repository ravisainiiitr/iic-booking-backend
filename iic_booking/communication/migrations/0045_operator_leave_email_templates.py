from django.db import migrations

_COMM_TYPE_EMAIL = "email"


def _upsert_template(CommunicationTemplate, *, name, code, subject, body_text, description, variable_help):
    obj, created = CommunicationTemplate.objects.get_or_create(
        code=code,
        defaults={
            "name": name,
            "communication_type": _COMM_TYPE_EMAIL,
            "subject": subject,
            "body_text": body_text,
            "description": description,
            "variable_help": variable_help,
            "is_active": True,
        },
    )
    if created:
        return

    # Keep existing customizations if admin has edited; only fill blanks.
    update_fields = []
    if not obj.name:
        obj.name = name
        update_fields.append("name")
    if obj.communication_type != _COMM_TYPE_EMAIL:
        obj.communication_type = _COMM_TYPE_EMAIL
        update_fields.append("communication_type")
    if not obj.subject:
        obj.subject = subject
        update_fields.append("subject")
    if not obj.body_text:
        obj.body_text = body_text
        update_fields.append("body_text")
    if not obj.description:
        obj.description = description
        update_fields.append("description")
    if not obj.variable_help:
        obj.variable_help = variable_help
        update_fields.append("variable_help")
    if not obj.is_active:
        obj.is_active = True
        update_fields.append("is_active")

    if update_fields:
        obj.save(update_fields=update_fields)


def create_operator_leave_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    _upsert_template(
        CommunicationTemplate,
        name="Operator Leave Submitted (to operator)",
        code="operator_leave_submitted_operator_email",
        subject="Leave request submitted ({{ start_date }} to {{ end_date }})",
        body_text=(
            "Hello {{ operator_name }},\n\n"
            "Your leave request has been submitted successfully.\n\n"
            "Dates: {{ start_date }} ({{ start_session }}) to {{ end_date }} ({{ end_session }})\n"
            "Reason: {{ reason }}\n\n"
            "Status: Pending approval\n\n"
            "Thanks,\n"
            "{{ app_name }}\n"
        ),
        description="Sent to operator when a leave request is submitted.",
        variable_help=(
            "Variables:\n"
            "- {{ app_name }}\n"
            "- {{ operator_name }}\n"
            "- {{ start_date }}\n"
            "- {{ start_session }} (FN/AN)\n"
            "- {{ end_date }}\n"
            "- {{ end_session }} (FN/AN)\n"
            "- {{ reason }}\n"
            "- {{ leave_id }}\n"
        ),
    )

    _upsert_template(
        CommunicationTemplate,
        name="Operator Leave Submitted (to OIC)",
        code="operator_leave_submitted_oic_email",
        subject="Leave approval needed: {{ operator_name }} ({{ start_date }} to {{ end_date }})",
        body_text=(
            "Hello {{ oic_name }},\n\n"
            "A leave request has been submitted and is pending your review.\n\n"
            "Operator: {{ operator_name }}\n"
            "Dates: {{ start_date }} ({{ start_session }}) to {{ end_date }} ({{ end_session }})\n"
            "Reason: {{ reason }}\n\n"
            "Please review and take action in the portal.\n\n"
            "Thanks,\n"
            "{{ app_name }}\n"
        ),
        description="Sent to OIC(s)/Managers when an operator submits a leave request.",
        variable_help=(
            "Variables:\n"
            "- {{ app_name }}\n"
            "- {{ oic_name }}\n"
            "- {{ operator_name }}\n"
            "- {{ start_date }}\n"
            "- {{ start_session }} (FN/AN)\n"
            "- {{ end_date }}\n"
            "- {{ end_session }} (FN/AN)\n"
            "- {{ reason }}\n"
            "- {{ leave_id }}\n"
        ),
    )

    _upsert_template(
        CommunicationTemplate,
        name="Operator Leave Approved (to operator)",
        code="operator_leave_approved_operator_email",
        subject="Leave request approved ({{ start_date }} to {{ end_date }})",
        body_text=(
            "Hello {{ operator_name }},\n\n"
            "Your leave request has been approved.\n\n"
            "Dates: {{ start_date }} ({{ start_session }}) to {{ end_date }} ({{ end_session }})\n"
            "Approved by: {{ reviewer_name }}\n\n"
            "Thanks,\n"
            "{{ app_name }}\n"
        ),
        description="Sent to operator when their leave request is approved.",
        variable_help=(
            "Variables:\n"
            "- {{ app_name }}\n"
            "- {{ operator_name }}\n"
            "- {{ reviewer_name }}\n"
            "- {{ start_date }}\n"
            "- {{ start_session }} (FN/AN)\n"
            "- {{ end_date }}\n"
            "- {{ end_session }} (FN/AN)\n"
            "- {{ leave_id }}\n"
        ),
    )

    _upsert_template(
        CommunicationTemplate,
        name="Operator Leave Rejected (to operator)",
        code="operator_leave_rejected_operator_email",
        subject="Leave request rejected ({{ start_date }} to {{ end_date }})",
        body_text=(
            "Hello {{ operator_name }},\n\n"
            "Your leave request has been rejected.\n\n"
            "Dates: {{ start_date }} ({{ start_session }}) to {{ end_date }} ({{ end_session }})\n"
            "Rejected by: {{ reviewer_name }}\n"
            "Reason for rejection: {{ rejection_reason }}\n\n"
            "Thanks,\n"
            "{{ app_name }}\n"
        ),
        description="Sent to operator when their leave request is rejected.",
        variable_help=(
            "Variables:\n"
            "- {{ app_name }}\n"
            "- {{ operator_name }}\n"
            "- {{ reviewer_name }}\n"
            "- {{ start_date }}\n"
            "- {{ start_session }} (FN/AN)\n"
            "- {{ end_date }}\n"
            "- {{ end_session }} (FN/AN)\n"
            "- {{ rejection_reason }}\n"
            "- {{ leave_id }}\n"
        ),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0044_booking_reminder_equipment_email_extra"),
    ]

    operations = [
        migrations.RunPython(create_operator_leave_templates, migrations.RunPython.noop),
    ]


# Add expected_duty_time to TA nomination call email template

from django.db import migrations


def update_ta_call_template(apps, schema_editor):
    """Add expected_duty_time to TA operating nomination call email template."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    t = CommunicationTemplate.objects.filter(code="ta_operating_nomination_call_email").first()
    if not t:
        return
    old_block = "Expected duty hours:\n{{ expected_duty_hours }}\n\nBenefits:"
    new_block = "Expected duty hours:\n{{ expected_duty_hours }}\n\nExpected duty time (from–to):\n{{ expected_duty_time }}\n\nBenefits:"
    if old_block in t.body_text and new_block not in t.body_text:
        t.body_text = t.body_text.replace(old_block, new_block)
        t.variable_help = (
            "Variables: {{ faculty_name }}, {{ faculty_email }}, {{ instrument_name }}, {{ instrument_code }}, "
            "{{ semester_name }}, {{ number_of_operators_required }}, {{ eligibility_criteria }}, "
            "{{ expected_duty_hours }}, {{ expected_duty_time }}, {{ benefits }}, {{ nomination_deadline }}, {{ portal_url }}."
        )
        t.save(update_fields=["body_text", "variable_help"])


def reverse_update(apps, schema_editor):
    """Remove expected_duty_time block from template (reverse)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    t = CommunicationTemplate.objects.filter(code="ta_operating_nomination_call_email").first()
    if not t:
        return
    new_block = "Expected duty hours:\n{{ expected_duty_hours }}\n\nExpected duty time (from–to):\n{{ expected_duty_time }}\n\nBenefits:"
    old_block = "Expected duty hours:\n{{ expected_duty_hours }}\n\nBenefits:"
    if new_block in t.body_text:
        t.body_text = t.body_text.replace(new_block, old_block)
        t.variable_help = (
            "Variables: {{ faculty_name }}, {{ faculty_email }}, {{ instrument_name }}, {{ instrument_code }}, "
            "{{ semester_name }}, {{ number_of_operators_required }}, {{ eligibility_criteria }}, "
            "{{ expected_duty_hours }}, {{ benefits }}, {{ nomination_deadline }}, {{ portal_url }}."
        )
        t.save(update_fields=["body_text", "variable_help"])


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0020_add_nomination_outcome_student_emails"),
    ]

    operations = [
        migrations.RunPython(update_ta_call_template, reverse_update),
    ]

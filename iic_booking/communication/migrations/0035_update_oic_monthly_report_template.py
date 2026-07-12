# Update oic_monthly_report template: equipment_name, lab operators, performance wording

from django.db import migrations


# Keep subject ≤255 chars when rendered; full name is in the body.
_NEW_SUBJECT = "Equipment performance report — {{ equipment_codes }} — {{ date_from }} to {{ date_to }}"

_NEW_BODY_TEXT = """Hello,

Please find attached the monthly equipment performance report for {{ equipment_name }} (equipment code: {{ equipment_codes }}) for the period {{ date_from }} to {{ date_to }}.

This email is sent to Officers in charge and Lab operators associated with this equipment. The PDF includes users served, samples (from booking input A), booking hours, working-window availability and utilization, weekend and holiday slot hours, disruption metrics (maintenance, operator absent, booking not utilized, other disruption), and consolidated user ratings where applicable.

Institute Instrumentation Centre
Indian Institute of Technology Roorkee

— IIC Booking"""

_NEW_BODY_HTML = """<p>Hello,</p>
<p>Please find attached the <strong>monthly equipment performance report</strong> for
<strong>{{ equipment_name }}</strong> (equipment code: <strong>{{ equipment_codes }}</strong>)
for the period <strong>{{ date_from }}</strong> to <strong>{{ date_to }}</strong>.</p>
<p>This email is sent to <strong>Officers in charge</strong> and <strong>Lab operators</strong>
associated with this equipment. The PDF includes users served, samples (from booking input A),
booking hours, working-window availability and utilization, weekend and holiday slot hours,
disruption metrics, and consolidated user ratings where applicable.</p>
<p style="color:#374151;font-size:13px;">Institute Instrumentation Centre<br/>
Indian Institute of Technology Roorkee</p>
<p>— IIC Booking</p>"""

_NEW_DESCRIPTION = (
    "Sent on the 1st of each month (or when scheduled) with one PDF per equipment to each linked "
    "Officer in charge and Lab operator. Schedule: equipment.send_oic_monthly_reports. "
    "Editable in Admin Settings > Communication."
)

_NEW_VARIABLE_HELP = (
    "Variables: {{ date_from }}, {{ date_to }}, {{ equipment_codes }} (code), {{ equipment_name }} (display name)."
)

_NEW_NAME = "OIC / Lab operator monthly equipment performance report"

_OLD_SUBJECT = "Equipment Utilization Report – {{ date_from }} to {{ date_to }}"
_OLD_BODY_TEXT = """Hello,

Please find attached the equipment utilization report for the period {{ date_from }} to {{ date_to }} for the equipment you are in charge of ({{ equipment_codes }}).

The report includes per-equipment booking counts, completed bookings, under maintenance and operator absent hours, booking not utilized, and slots with no booking. A pie chart of overall utilization is included.

— IIC Booking"""
_OLD_DESCRIPTION = (
    "Sent to each OIC (Officer in Charge) on the 1st of every month with PDF report for their equipment. "
    "Schedule: equipment.send_oic_monthly_reports. Editable in Admin Settings > Communication."
)
_OLD_VARIABLE_HELP = "Variables: {{ date_from }}, {{ date_to }}, {{ equipment_codes }}."
_OLD_NAME = "OIC Monthly Equipment Report"


def update_oic_monthly_report_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    tpl = CommunicationTemplate.objects.filter(
        code="oic_monthly_report",
        communication_type="email",
    ).first()
    if not tpl:
        return
    tpl.name = _NEW_NAME
    tpl.subject = _NEW_SUBJECT
    tpl.body_text = _NEW_BODY_TEXT
    tpl.body_html = _NEW_BODY_HTML
    tpl.description = _NEW_DESCRIPTION
    tpl.variable_help = _NEW_VARIABLE_HELP
    tpl.save()


def revert_oic_monthly_report_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    tpl = CommunicationTemplate.objects.filter(
        code="oic_monthly_report",
        communication_type="email",
    ).first()
    if not tpl:
        return
    tpl.name = _OLD_NAME
    tpl.subject = _OLD_SUBJECT
    tpl.body_text = _OLD_BODY_TEXT
    tpl.body_html = ""
    tpl.description = _OLD_DESCRIPTION
    tpl.variable_help = _OLD_VARIABLE_HELP
    tpl.save()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0034_add_sample_disposed_email_template"),
    ]

    operations = [
        migrations.RunPython(update_oic_monthly_report_template, revert_oic_monthly_report_template),
    ]

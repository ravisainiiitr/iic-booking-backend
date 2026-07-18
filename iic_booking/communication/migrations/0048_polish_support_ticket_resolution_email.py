from django.db import migrations


def polish_resolution_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    tpl = CommunicationTemplate.objects.filter(code="support_ticket_resolution_email").first()
    if not tpl:
        return
    tpl.subject = "Your support ticket #{{ ticket_id }} has been {{ status_display }}"
    tpl.body_text = """Hello {{ user_name }},

Thank you for contacting IIT Roorkee IIC support. Your ticket has been marked as {{ status_display }}.

Ticket ID: #{{ ticket_id }}
Subject: {{ subject }}
Status: {{ status_display }}

Resolution comments:
{{ resolution_notes }}

If you still need help, reply on the ticket from your Tickets page or raise a new support request.

{{ link }}

Kind regards,
IIT Roorkee IIC Support Team
"""
    tpl.body_html = """<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">
  <div style="max-width:600px;margin:0 auto;padding:20px;">
    <h2 style="color:#0f766e;">Support ticket {{ status_display }}</h2>
    <p>Hello {{ user_name }},</p>
    <p>Thank you for contacting IIT Roorkee IIC support. Your ticket has been marked as <strong>{{ status_display }}</strong>.</p>
    <div style="background:#f8fafc;border-left:4px solid #0f766e;padding:12px 16px;margin:16px 0;">
      <p><strong>Ticket ID:</strong> #{{ ticket_id }}</p>
      <p><strong>Subject:</strong> {{ subject }}</p>
      <p><strong>Status:</strong> {{ status_display }}</p>
      <p><strong>Resolution comments:</strong></p>
      <p style="white-space:pre-wrap;">{{ resolution_notes }}</p>
    </div>
    <p>If you still need help, open your Tickets page or raise a new support request.</p>
    <p>{{ link }}</p>
    <p>Kind regards,<br/>IIT Roorkee IIC Support Team</p>
  </div>
</body></html>"""
    tpl.description = (
        "Sent to the requester when a ticket is marked Resolved or Closed. "
        "Includes ticket id, subject, status, and resolution comments."
    )
    tpl.save()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0047_sample_submission_deadline_reminder_email"),
    ]

    operations = [
        migrations.RunPython(polish_resolution_template, noop_reverse),
    ]

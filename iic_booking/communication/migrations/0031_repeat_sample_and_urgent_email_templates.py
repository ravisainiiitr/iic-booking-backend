# Repeat sample + urgent booking: consistent subjects, bodies, and HTML styling.

from django.db import migrations


EMAIL_STYLE = """
        body { font-family: Arial, Helvetica, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; background-color: #f4f4f4; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { color: #ffffff; padding: 22px 20px; text-align: center; border-radius: 6px 6px 0 0; }
        .header.teal { background: linear-gradient(135deg, #00695c 0%, #00897b 100%); }
        .header.amber { background: linear-gradient(135deg, #e65100 0%, #fb8c00 100%); }
        .content { background-color: #ffffff; padding: 22px; border-radius: 0 0 6px 6px; border: 1px solid #e0e0e0; border-top: none; }
        .booking-details { background-color: #fafafa; padding: 16px; margin: 16px 0; border-left: 4px solid #00695c; }
        .booking-details.amber { border-left-color: #e65100; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #444; }
        .lead { font-size: 15px; color: #444; }
        .footer { text-align: center; margin-top: 22px; color: #777; font-size: 12px; }
"""


def _html_shell(inner_body):
    return (
        """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>"""
        + EMAIL_STYLE
        + """</style>
</head>
<body>
"""
        + inner_body
        + """
</body>
</html>"""
    )


def upsert_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    CommunicationTemplate.objects.update_or_create(
        code="repeat_sample_booking_confirmed_email",
        defaults={
            "name": "Repeat sample booking confirmed",
            "communication_type": "email",
            "subject": "Repeat Sample Request Approved – {{ equipment_name }} ({{ booking_id }})",
            "body_text": """Hello {{ user_name }},

Your repeat sample request has been approved, and a complimentary repeat booking has been created for you.

Booking details:
- New booking ID: {{ booking_id }}
- Original booking reference: {{ original_booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Start time: {{ start_time }}
- End time: {{ end_time }}
- Duration: {{ total_hours }} hours
- Total charge: ₹{{ total_charge }}
- Final amount remaining in wallet: {{ wallet_balance_after }}

{{ comment }}

View your booking: {{ link }}

Thank you for using the IIC Booking System.""",
            "body_html": _html_shell(
                """
    <div class="container">
        <div class="header teal">
            <h2 style="margin:0;font-size:20px;">Repeat Sample Request Approved</h2>
            <p style="margin:8px 0 0;font-size:14px;opacity:0.95;">Your complimentary repeat booking is confirmed.</p>
        </div>
        <div class="content">
            <p class="lead">Hello {{ user_name }},</p>
            <p>Your <strong>repeat sample</strong> request has been <strong>approved</strong>. A complimentary repeat booking has been created using the details below.</p>
            <div class="booking-details">
                <h3 style="margin-top:0;color:#00695c;">Booking details</h3>
                <div class="detail-row"><span class="label">New booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Original booking:</span> {{ original_booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Start time:</span> {{ start_time }}</div>
                <div class="detail-row"><span class="label">End time:</span> {{ end_time }}</div>
                <div class="detail-row"><span class="label">Duration:</span> {{ total_hours }} hours</div>
                <div class="detail-row"><span class="label">Total charge:</span> ₹{{ total_charge }}</div>
                <div class="detail-row"><span class="label">Final amount remaining in wallet:</span> {{ wallet_balance_after }}</div>
            </div>
            <p><strong>Note:</strong> {{ comment }}</p>
            <p>View your booking: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>Thank you for using the IIC Booking System.</p>
            </div>
        </div>
    </div>
"""
            ),
            "description": "Sent when a repeat sample booking is created (admin-approved request or user repeat flow).",
            "variable_help": (
                "{{ user_name }}, {{ booking_id }}, {{ original_booking_id }}, {{ equipment_name }}, {{ equipment_code }}, "
                "{{ start_time }}, {{ end_time }}, {{ total_hours }}, {{ total_charge }}, {{ wallet_balance_after }}, "
                "{{ comment }}, {{ link }}"
            ),
            "is_active": True,
        },
    )

    CommunicationTemplate.objects.update_or_create(
        code="urgent_booking_request_submitted_user_email",
        defaults={
            "name": "Urgent booking request submitted (user)",
            "communication_type": "email",
            "subject": "Urgent Booking Request Submitted – {{ equipment_name }}",
            "body_text": """Hello {{ user_name }},

We have received your urgent booking request.

Request ID: {{ request_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})
Request type: {{ request_type_label }}

{{ next_steps }}

Track your request: {{ link }}

— IIC Booking""",
            "body_html": _html_shell(
                """
    <div class="container">
        <div class="header amber">
            <h2 style="margin:0;font-size:20px;">Urgent Booking Request Submitted</h2>
            <p style="margin:8px 0 0;font-size:14px;opacity:0.95;">We have received your request.</p>
        </div>
        <div class="content">
            <p class="lead">Hello {{ user_name }},</p>
            <p>Your <strong>urgent booking request</strong> has been submitted successfully.</p>
            <div class="booking-details amber">
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Request type:</span> {{ request_type_label }}</div>
            </div>
            <p>{{ next_steps }}</p>
            <p>Track your request: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>IIC Booking System</p>
            </div>
        </div>
    </div>
"""
            ),
            "description": "Sent to the requester when an urgent booking request is submitted.",
            "variable_help": "{{ user_name }}, {{ request_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ request_type_label }}, {{ next_steps }}, {{ link }}",
            "is_active": True,
        },
    )

    CommunicationTemplate.objects.update_or_create(
        code="urgent_booking_supervisor_decision_user_email",
        defaults={
            "name": "Urgent booking — supervisor decision (user)",
            "communication_type": "email",
            "subject": "Urgent Booking Request {{ decision_phrase }} – {{ equipment_name }}",
            "body_text": """Hello {{ user_name }},

Your urgent booking request was reviewed by your supervisor.

Request ID: {{ request_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})
Outcome: {{ decision_summary }}

Supervisor: {{ supervisor_name }}
Notes: {{ supervisor_notes }}

{{ next_steps }}

View details: {{ link }}

— IIC Booking""",
            "body_html": _html_shell(
                """
    <div class="container">
        <div class="header amber">
            <h2 style="margin:0;font-size:20px;">Urgent Booking Request {{ decision_phrase }}</h2>
            <p style="margin:8px 0 0;font-size:14px;opacity:0.95;">Supervisor review</p>
        </div>
        <div class="content">
            <p class="lead">Hello {{ user_name }},</p>
            <p>{{ decision_summary }}</p>
            <div class="booking-details amber">
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Supervisor:</span> {{ supervisor_name }}</div>
                <div class="detail-row"><span class="label">Notes:</span> {{ supervisor_notes }}</div>
            </div>
            <p>{{ next_steps }}</p>
            <p>View details: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>IIC Booking System</p>
            </div>
        </div>
    </div>
"""
            ),
            "description": "Sent to the requester when the supervisor approves or rejects a reviewer-urgent request.",
            "variable_help": (
                "{{ user_name }}, {{ request_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ decision_phrase }}, "
                "{{ decision_summary }}, {{ supervisor_name }}, {{ supervisor_notes }}, {{ next_steps }}, {{ link }}"
            ),
            "is_active": True,
        },
    )

    CommunicationTemplate.objects.update_or_create(
        code="urgent_booking_hold_confirmed_email",
        defaults={
            "name": "Urgent booking — hold confirmed",
            "communication_type": "email",
            "subject": "Urgent Booking Request Approved by Admin – Booking Confirmed – {{ equipment_name }}",
            "body_text": """Hello {{ user_name }},

Your urgent booking request was approved by the administrator, and your held slots are now confirmed as a booking.

Booking ID: {{ booking_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})
Previous status: {{ previous_status }}
New status: {{ new_status }}

{{ comment }}

View your booking: {{ link }}

— IIC Booking""",
            "body_html": _html_shell(
                """
    <div class="container">
        <div class="header amber">
            <h2 style="margin:0;font-size:20px;">Urgent Booking Request Approved by Admin</h2>
            <p style="margin:8px 0 0;font-size:14px;opacity:0.95;">Your hold is now a confirmed booking.</p>
        </div>
        <div class="content">
            <p class="lead">Hello {{ user_name }},</p>
            <p>Your <strong>urgent booking request</strong> was <strong>approved by the administrator</strong>. Your held time has been <strong>confirmed</strong>.</p>
            <div class="booking-details amber">
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Previous status:</span> {{ previous_status }}</div>
                <div class="detail-row"><span class="label">New status:</span> {{ new_status }}</div>
            </div>
            <p><strong>Note:</strong> {{ comment }}</p>
            <p>View your booking: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>IIC Booking System</p>
            </div>
        </div>
    </div>
"""
            ),
            "description": "Sent when an urgent request is approved and a HOLD booking is converted to BOOKED.",
            "variable_help": (
                "{{ user_name }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ previous_status }}, "
                "{{ new_status }}, {{ comment }}, {{ link }}"
            ),
            "is_active": True,
        },
    )

    CommunicationTemplate.objects.update_or_create(
        code="urgent_booking_hold_released_email",
        defaults={
            "name": "Urgent booking — hold released",
            "communication_type": "email",
            "subject": "Urgent Booking Request Update – Hold Released – {{ equipment_name }}",
            "body_text": """Hello {{ user_name }},

Your hold has been released in connection with your urgent booking request. The booking is no longer active.

Booking ID: {{ booking_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})
Previous status: {{ previous_status }}
New status: {{ new_status }}

{{ comment }}

View details: {{ link }}

— IIC Booking""",
            "body_html": _html_shell(
                """
    <div class="container">
        <div class="header amber">
            <h2 style="margin:0;font-size:20px;">Urgent Booking Request Update</h2>
            <p style="margin:8px 0 0;font-size:14px;opacity:0.95;">Hold released</p>
        </div>
        <div class="content">
            <p class="lead">Hello {{ user_name }},</p>
            <p>Your <strong>hold</strong> linked to an <strong>urgent booking request</strong> has been <strong>released</strong>. The booking is <strong>cancelled</strong>, and the slots have been freed.</p>
            <div class="booking-details amber">
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Previous status:</span> {{ previous_status }}</div>
                <div class="detail-row"><span class="label">New status:</span> {{ new_status }}</div>
            </div>
            <p><strong>Note:</strong> {{ comment }}</p>
            <p>View details: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>IIC Booking System</p>
            </div>
        </div>
    </div>
"""
            ),
            "description": "Sent when a HOLD is released due to urgent request rejection or expiry.",
            "variable_help": (
                "{{ user_name }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ previous_status }}, "
                "{{ new_status }}, {{ comment }}, {{ link }}"
            ),
            "is_active": True,
        },
    )

    CommunicationTemplate.objects.update_or_create(
        code="urgent_booking_admin_decision_user_email",
        defaults={
            "name": "Urgent booking — admin/OIC decision (user)",
            "communication_type": "email",
            "subject": "{{ decision_headline }} – {{ equipment_name }}",
            "body_text": """Hello {{ user_name }},

{{ decision_body }}

Request ID: {{ request_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})

Admin notes: {{ admin_notes }}

View your requests: {{ link }}

— IIC Booking""",
            "body_html": _html_shell(
                """
    <div class="container">
        <div class="header amber">
            <h2 style="margin:0;font-size:20px;">{{ decision_headline }}</h2>
            <p style="margin:8px 0 0;font-size:14px;opacity:0.95;">Urgent booking request</p>
        </div>
        <div class="content">
            <p class="lead">Hello {{ user_name }},</p>
            <p>{{ decision_body }}</p>
            <div class="booking-details amber">
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Admin notes:</span> {{ admin_notes }}</div>
            </div>
            <p>View your requests: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>IIC Booking System</p>
            </div>
        </div>
    </div>
"""
            ),
            "description": "Sent when Admin/OIC approves or rejects an urgent request without a hold conversion email.",
            "variable_help": (
                "{{ user_name }}, {{ decision_headline }}, {{ decision_body }}, {{ request_id }}, {{ equipment_name }}, "
                "{{ equipment_code }}, {{ admin_notes }}, {{ link }}"
            ),
            "is_active": True,
        },
    )

    t = CommunicationTemplate.objects.filter(code="urgent_reviewer_pending_supervisor_email").first()
    if t:
        t.name = "Urgent booking — supervisor action required"
        t.subject = "Urgent Booking Request — Supervisor Action Required – {{ equipment_name }}"
        t.body_text = """Hello {{ supervisor_name }},

{{ requester_name }} ({{ requester_email }}) has submitted an urgent booking request with documentary evidence (reviewer comment).

Request ID: {{ request_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})

Please sign in to the IIC Booking portal and approve or reject this request in your supervisor queue before an administrator can review it.

Open supervisor urgent requests: {{ link }}

If you did not expect this message, please contact the IIC booking desk.

— IIC Booking"""
        t.body_html = _html_shell(
            """
    <div class="container">
        <div class="header amber">
            <h2 style="margin:0;font-size:20px;">Urgent Booking Request — Supervisor Action Required</h2>
            <p style="margin:8px 0 0;font-size:14px;opacity:0.95;">A student or user needs your approval to proceed.</p>
        </div>
        <div class="content">
            <p class="lead">Hello {{ supervisor_name }},</p>
            <p><strong>{{ requester_name }}</strong> ({{ requester_email }}) has submitted an <strong>urgent booking request</strong> with <strong>reviewer evidence</strong>.</p>
            <div class="booking-details amber">
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
            </div>
            <p>Please review and <strong>approve or reject</strong> this request in your <strong>supervisor queue</strong> before an administrator can take further action.</p>
            <p><a href="{{ link }}">Open supervisor urgent requests</a></p>
            <p style="font-size:13px;color:#666;">If you did not expect this message, please contact the IIC booking desk.</p>
            <div class="footer">
                <p>IIC Booking System</p>
            </div>
        </div>
    </div>
"""
        )
        t.description = (
            "Sent to the wallet owner (supervisor) when a user submits a REVIEWER_URGENT urgent booking request."
        )
        t.variable_help = (
            "Variables: {{ supervisor_name }}, {{ requester_name }}, {{ requester_email }}, "
            "{{ equipment_name }}, {{ equipment_code }}, {{ request_id }}, {{ link }}."
        )
        t.save(
            update_fields=[
                "name",
                "subject",
                "body_text",
                "body_html",
                "description",
                "variable_help",
            ]
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0030_remove_virtual_booking_id_from_booking_created_email"),
    ]

    operations = [
        migrations.RunPython(upsert_templates, noop_reverse),
    ]

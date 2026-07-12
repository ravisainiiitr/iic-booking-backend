# Generated manually to add wallet join request email templates

from django.db import migrations


def create_wallet_join_request_templates(apps, schema_editor):
    """Create sample email templates for wallet join request (submit, approve, reject, cancel, remove)."""
    CommunicationTemplate = apps.get_model('communication', 'CommunicationTemplate')

    templates = [
        # Wallet Join Request Submitted (to faculty – new request from student)
        {
            'name': 'Wallet Join Request Submitted Email',
            'code': 'wallet_join_request_submitted_email',
            'communication_type': 'email',
            'subject': 'New Wallet Join Request from {{ student_name }}',
            'body_text': '''Hello {{ faculty_name }},

A student has requested to join your wallet.

Request Details:
- Request ID: {{ request_id }}
- Student Name: {{ student_name }}
- Student Email: {{ student_email }}
- Request Date: {{ request_date }}
- Message: {{ message }}

Please approve or reject this request from your wallet dashboard.

View request: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #2196F3; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #2196F3; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>New Wallet Join Request</h2>
        </div>
        <div class="content">
            <p>Hello {{ faculty_name }},</p>
            <p>A student has requested to join your wallet.</p>
            <div class="details">
                <h3>Request Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Student Name:</span> {{ student_name }}</div>
                <div class="detail-row"><span class="label">Student Email:</span> {{ student_email }}</div>
                <div class="detail-row"><span class="label">Request Date:</span> {{ request_date }}</div>
                <div class="detail-row"><span class="label">Message:</span> {{ message }}</div>
            </div>
            <p>Please approve or reject this request from your wallet dashboard.</p>
            <p>View request: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent to faculty when a student submits a wallet join request',
            'variable_help': 'Available: {{ faculty_name }}, {{ faculty_email }}, {{ student_name }}, {{ student_email }}, {{ request_id }}, {{ request_date }}, {{ message }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Join Request Approved (to student)
        {
            'name': 'Wallet Join Request Approved Email',
            'code': 'wallet_join_request_approved_email',
            'communication_type': 'email',
            'subject': 'Wallet Join Request Approved',
            'body_text': '''Hello {{ student_name }},

Your request to join the wallet of {{ faculty_name }} has been approved.

Request Details:
- Request ID: {{ request_id }}
- Faculty: {{ faculty_name }} ({{ faculty_email }})
- Approved On: {{ responded_at }}
- Response: {{ faculty_response }}

You can now use the wallet for equipment bookings.

View your wallet: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #4CAF50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Wallet Join Request Approved</h2>
        </div>
        <div class="content">
            <p>Hello {{ student_name }},</p>
            <p>Your request to join the wallet of <strong>{{ faculty_name }}</strong> has been approved.</p>
            <div class="details">
                <h3>Request Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Faculty:</span> {{ faculty_name }} ({{ faculty_email }})</div>
                <div class="detail-row"><span class="label">Approved On:</span> {{ responded_at }}</div>
                <div class="detail-row"><span class="label">Response:</span> {{ faculty_response }}</div>
            </div>
            <p>You can now use the wallet for equipment bookings.</p>
            <p>View your wallet: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent to student when faculty approves their wallet join request',
            'variable_help': 'Available: {{ student_name }}, {{ student_email }}, {{ faculty_name }}, {{ faculty_email }}, {{ request_id }}, {{ responded_at }}, {{ faculty_response }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Join Request Rejected (to student)
        {
            'name': 'Wallet Join Request Rejected Email',
            'code': 'wallet_join_request_rejected_email',
            'communication_type': 'email',
            'subject': 'Wallet Join Request Not Approved',
            'body_text': '''Hello {{ student_name }},

Your request to join the wallet of {{ faculty_name }} was not approved.

Request Details:
- Request ID: {{ request_id }}
- Faculty: {{ faculty_name }} ({{ faculty_email }})
- Responded On: {{ responded_at }}
- Response: {{ faculty_response }}

You may request to join another faculty wallet from your wallet dashboard.

View requests: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #f44336; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #f44336; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Wallet Join Request Not Approved</h2>
        </div>
        <div class="content">
            <p>Hello {{ student_name }},</p>
            <p>Your request to join the wallet of <strong>{{ faculty_name }}</strong> was not approved.</p>
            <div class="details">
                <h3>Request Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Faculty:</span> {{ faculty_name }} ({{ faculty_email }})</div>
                <div class="detail-row"><span class="label">Responded On:</span> {{ responded_at }}</div>
                <div class="detail-row"><span class="label">Response:</span> {{ faculty_response }}</div>
            </div>
            <p>You may request to join another faculty wallet from your wallet dashboard.</p>
            <p>View requests: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent to student when faculty rejects their wallet join request',
            'variable_help': 'Available: {{ student_name }}, {{ student_email }}, {{ faculty_name }}, {{ faculty_email }}, {{ request_id }}, {{ responded_at }}, {{ faculty_response }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Join Request Cancelled (to faculty – student cancelled)
        {
            'name': 'Wallet Join Request Cancelled Email',
            'code': 'wallet_join_request_cancelled_email',
            'communication_type': 'email',
            'subject': 'Wallet Join Request Cancelled by {{ student_name }}',
            'body_text': '''Hello {{ faculty_name }},

A wallet join request has been cancelled by the student.

Request Details:
- Request ID: {{ request_id }}
- Student Name: {{ student_name }}
- Student Email: {{ student_email }}
- Request Date: {{ request_date }}
- Cancelled On: {{ responded_at }}

View your wallet join requests: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #FF9800; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #FF9800; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Wallet Join Request Cancelled</h2>
        </div>
        <div class="content">
            <p>Hello {{ faculty_name }},</p>
            <p>A wallet join request has been cancelled by the student.</p>
            <div class="details">
                <h3>Request Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Student Name:</span> {{ student_name }}</div>
                <div class="detail-row"><span class="label">Student Email:</span> {{ student_email }}</div>
                <div class="detail-row"><span class="label">Request Date:</span> {{ request_date }}</div>
                <div class="detail-row"><span class="label">Cancelled On:</span> {{ responded_at }}</div>
            </div>
            <p>View your wallet join requests: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent to faculty when a student cancels their wallet join request',
            'variable_help': 'Available: {{ faculty_name }}, {{ faculty_email }}, {{ student_name }}, {{ student_email }}, {{ request_id }}, {{ request_date }}, {{ responded_at }}, {{ link }}',
            'is_active': True,
        },
        # Student Removed from Wallet (to student)
        {
            'name': 'Wallet Join Request Removed Email',
            'code': 'wallet_join_request_removed_email',
            'communication_type': 'email',
            'subject': "You have been removed from {{ faculty_name }}'s wallet",
            'body_text': '''Hello {{ student_name }},

You have been removed from the wallet of {{ faculty_name }}.

Request Details:
- Request ID: {{ request_id }}
- Faculty: {{ faculty_name }} ({{ faculty_email }})
- Removed On: {{ responded_at }}
- Message: {{ faculty_response }}

You can request to join another faculty wallet from your wallet dashboard.

View wallet: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #9E9E9E; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #9E9E9E; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Removed from Wallet</h2>
        </div>
        <div class="content">
            <p>Hello {{ student_name }},</p>
            <p>You have been removed from the wallet of <strong>{{ faculty_name }}</strong>.</p>
            <div class="details">
                <h3>Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Faculty:</span> {{ faculty_name }} ({{ faculty_email }})</div>
                <div class="detail-row"><span class="label">Removed On:</span> {{ responded_at }}</div>
                <div class="detail-row"><span class="label">Message:</span> {{ faculty_response }}</div>
            </div>
            <p>You can request to join another faculty wallet from your wallet dashboard.</p>
            <p>View wallet: <a href="{{ link }}">{{ link }}</a></p>
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent to student when faculty removes them from the wallet',
            'variable_help': 'Available: {{ student_name }}, {{ student_email }}, {{ faculty_name }}, {{ faculty_email }}, {{ request_id }}, {{ responded_at }}, {{ faculty_response }}, {{ link }}',
            'is_active': True,
        },
    ]

    for template_data in templates:
        if not CommunicationTemplate.objects.filter(code=template_data['code']).exists():
            CommunicationTemplate.objects.create(**template_data)


def remove_wallet_join_request_templates(apps, schema_editor):
    """Remove wallet join request email templates (reverse migration)."""
    CommunicationTemplate = apps.get_model('communication', 'CommunicationTemplate')
    template_codes = [
        'wallet_join_request_submitted_email',
        'wallet_join_request_approved_email',
        'wallet_join_request_rejected_email',
        'wallet_join_request_cancelled_email',
        'wallet_join_request_removed_email',
    ]
    CommunicationTemplate.objects.filter(code__in=template_codes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('communication', '0006_add_booking_and_wallet_email_templates'),
    ]

    operations = [
        migrations.RunPython(create_wallet_join_request_templates, remove_wallet_join_request_templates),
    ]

# Generated manually to add wallet recharge request email template (for accounts team)

from django.db import migrations


def create_wallet_recharge_request_template(apps, schema_editor):
    """Create email template for wallet recharge request notification to accounts team."""
    CommunicationTemplate = apps.get_model('communication', 'CommunicationTemplate')

    if CommunicationTemplate.objects.filter(code='wallet_recharge_request_email').exists():
        return

    CommunicationTemplate.objects.create(
        name='Wallet Recharge Request Email (Accounts)',
        code='wallet_recharge_request_email',
        communication_type='email',
        subject='Wallet Recharge Request - ₹{{ amount }} - {{ user_email }}',
        body_text='''A new wallet recharge request has been submitted.

Request Details:
- Request ID: {{ request_id }}
- User Name: {{ user_name }}
- User Email: {{ user_email }}
- Amount: ₹{{ amount }}
- Department: {{ department_name }} ({{ department_code }})
- Request Date: {{ request_date }}
- Project Name: {{ project_name }}
- Project Code: {{ project_code }}
- Agency: {{ project_agency }}
- Project Details: {{ project_details }}

Approve: {{ approve_url }}
Reject: {{ reject_url }}

Please process this request from the link above.

Thank you for using IIC Booking System!''',
        body_html='''<!DOCTYPE html>
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
        .actions { margin: 20px 0; }
        .btn { display: inline-block; padding: 12px 24px; margin: 5px; text-decoration: none; border-radius: 5px; font-weight: bold; }
        .btn-approve { background-color: #4CAF50; color: white; }
        .btn-reject { background-color: #f44336; color: white; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>New Wallet Recharge Request</h2>
        </div>
        <div class="content">
            <p>A new wallet recharge request has been submitted and requires your action.</p>
            
            <div class="details">
                <h3>Request Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">User Name:</span> {{ user_name }}</div>
                <div class="detail-row"><span class="label">User Email:</span> {{ user_email }}</div>
                <div class="detail-row"><span class="label">Amount:</span> ₹{{ amount }}</div>
                <div class="detail-row"><span class="label">Department:</span> {{ department_name }} ({{ department_code }})</div>
                <div class="detail-row"><span class="label">Request Date:</span> {{ request_date }}</div>
                <div class="detail-row"><span class="label">Project Name:</span> {{ project_name }}</div>
                <div class="detail-row"><span class="label">Project Code:</span> {{ project_code }}</div>
                <div class="detail-row"><span class="label">Agency:</span> {{ project_agency }}</div>
                <div class="detail-row"><span class="label">Project Details:</span> {{ project_details }}</div>
            </div>
            
            <div class="actions">
                <p><strong>Take action:</strong></p>
                <a href="{{ approve_url }}" class="btn btn-approve">Approve Request</a>
                <a href="{{ reject_url }}" class="btn btn-reject">Reject Request</a>
            </div>
            
            <p>Or use the links below:</p>
            <p>Approve: <a href="{{ approve_url }}">{{ approve_url }}</a></p>
            <p>Reject: <a href="{{ reject_url }}">{{ reject_url }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
        description='Email sent to accounts team when a user submits a wallet recharge request (contains approve/reject links)',
        variable_help='Available: {{ user_name }}, {{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, {{ department_name }}, {{ department_code }}, {{ project_name }}, {{ project_code }}, {{ project_agency }}, {{ project_details }}, {{ approve_url }}, {{ reject_url }}',
        is_active=True,
    )


def remove_wallet_recharge_request_template(apps, schema_editor):
    """Remove wallet recharge request email template (reverse migration)."""
    CommunicationTemplate = apps.get_model('communication', 'CommunicationTemplate')
    CommunicationTemplate.objects.filter(code='wallet_recharge_request_email').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('communication', '0007_add_wallet_join_request_email_templates'),
    ]

    operations = [
        migrations.RunPython(create_wallet_recharge_request_template, remove_wallet_recharge_request_template),
    ]

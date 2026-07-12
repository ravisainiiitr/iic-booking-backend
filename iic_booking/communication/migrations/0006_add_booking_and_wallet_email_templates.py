# Generated manually to add booking and wallet email templates

from django.db import migrations


def create_booking_wallet_templates(apps, schema_editor):
    """Create sample email templates for booking status changes and wallet transactions."""
    CommunicationTemplate = apps.get_model('communication', 'CommunicationTemplate')
    
    templates = [
        # Booking Created Email
        {
            'name': 'Booking Created Email',
            'code': 'booking_created_email',
            'communication_type': 'email',
            'subject': 'Booking Confirmed - {{ equipment_name }} (Booking #{{ booking_id }})',
            'body_text': '''Hello {{ user_name }},

Your equipment booking has been successfully created!

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Start Time: {{ start_time }}
- End Time: {{ end_time }}
- Duration: {{ total_hours }} hours
- Total Charge: ₹{{ total_charge }}

{{ comment }}

You can view your booking details at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #4CAF50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Booking Confirmed!</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your equipment booking has been successfully created!</p>
            
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Start Time:</span> {{ start_time }}</div>
                <div class="detail-row"><span class="label">End Time:</span> {{ end_time }}</div>
                <div class="detail-row"><span class="label">Duration:</span> {{ total_hours }} hours</div>
                <div class="detail-row"><span class="label">Total Charge:</span> ₹{{ total_charge }}</div>
            </div>
            
            <p><strong>Note:</strong> {{ comment }}</p>
            
            <p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when a new booking is created',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_hours }}, {{ total_charge }}, {{ comment }}, {{ link }}',
            'is_active': True,
        },
        # Booking Cancelled Email
        {
            'name': 'Booking Cancelled Email',
            'code': 'booking_cancelled_email',
            'communication_type': 'email',
            'subject': 'Booking Cancelled - {{ equipment_name }} (Booking #{{ booking_id }})',
            'body_text': '''Hello {{ user_name }},

Your booking has been cancelled.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Start Time: {{ start_time }}
- End Time: {{ end_time }}

{{ comment }}

You can view your booking details at: {{ link }}

If you have any questions, please contact us.

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #f44336; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #f44336; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Booking Cancelled</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your booking has been cancelled.</p>
            
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Start Time:</span> {{ start_time }}</div>
                <div class="detail-row"><span class="label">End Time:</span> {{ end_time }}</div>
            </div>
            
            <p><strong>Reason:</strong> {{ comment }}</p>
            
            <p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            <p>If you have any questions, please contact us.</p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when a booking is cancelled',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ comment }}, {{ link }}',
            'is_active': True,
        },
        # Booking Rescheduled Email
        {
            'name': 'Booking Rescheduled Email',
            'code': 'booking_rescheduled_email',
            'communication_type': 'email',
            'subject': 'Booking Rescheduled - {{ equipment_name }} (Booking #{{ booking_id }})',
            'body_text': '''Hello {{ user_name }},

Your booking has been rescheduled.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- New Start Time: {{ start_time }}
- New End Time: {{ end_time }}
- Duration: {{ total_hours }} hours

{{ comment }}

You can view your booking details at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #2196F3; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #2196F3; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Booking Rescheduled</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your booking has been rescheduled.</p>
            
            <div class="booking-details">
                <h3>Updated Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">New Start Time:</span> {{ start_time }}</div>
                <div class="detail-row"><span class="label">New End Time:</span> {{ end_time }}</div>
                <div class="detail-row"><span class="label">Duration:</span> {{ total_hours }} hours</div>
            </div>
            
            <p><strong>Note:</strong> {{ comment }}</p>
            
            <p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when a booking is rescheduled',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_hours }}, {{ comment }}, {{ link }}',
            'is_active': True,
        },
        # Booking Completed Email
        {
            'name': 'Booking Completed Email',
            'code': 'booking_completed_email',
            'communication_type': 'email',
            'subject': 'Booking Completed - {{ equipment_name }} (Booking #{{ booking_id }})',
            'body_text': '''Hello {{ user_name }},

Your booking has been marked as completed.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Start Time: {{ start_time }}
- End Time: {{ end_time }}
- Duration: {{ total_hours }} hours
- Total Charge: ₹{{ total_charge }}

{{ comment }}

You can view your booking details at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #4CAF50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Booking Completed</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your booking has been marked as completed.</p>
            
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Start Time:</span> {{ start_time }}</div>
                <div class="detail-row"><span class="label">End Time:</span> {{ end_time }}</div>
                <div class="detail-row"><span class="label">Duration:</span> {{ total_hours }} hours</div>
                <div class="detail-row"><span class="label">Total Charge:</span> ₹{{ total_charge }}</div>
            </div>
            
            <p><strong>Note:</strong> {{ comment }}</p>
            
            <p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when a booking is marked as completed',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_hours }}, {{ total_charge }}, {{ comment }}, {{ link }}',
            'is_active': True,
        },
        # Booking Refunded Email
        {
            'name': 'Booking Refunded Email',
            'code': 'booking_refunded_email',
            'communication_type': 'email',
            'subject': 'Booking Refunded - {{ equipment_name }} (Booking #{{ booking_id }})',
            'body_text': '''Hello {{ user_name }},

Your booking has been refunded.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Refund Amount: ₹{{ total_charge }}

{{ comment }}

The refund amount has been credited to your wallet. You can view your booking details at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #FF9800; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #FF9800; }
        .refund-amount { background-color: #E8F5E9; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; font-size: 18px; font-weight: bold; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Booking Refunded</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your booking has been refunded.</p>
            
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
            </div>
            
            <div class="refund-amount">
                Refund Amount: ₹{{ total_charge }}
            </div>
            
            <p><strong>Note:</strong> {{ comment }}</p>
            
            <p>The refund amount has been credited to your wallet. You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when a booking is refunded',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ total_charge }}, {{ comment }}, {{ link }}',
            'is_active': True,
        },
        # Booking Absent Email
        {
            'name': 'Booking Absent Email',
            'code': 'booking_absent_email',
            'communication_type': 'email',
            'subject': 'Booking Marked as Absent - {{ equipment_name }} (Booking #{{ booking_id }})',
            'body_text': '''Hello {{ user_name }},

Your booking has been marked as absent.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Start Time: {{ start_time }}
- End Time: {{ end_time }}

{{ comment }}

You can view your booking details at: {{ link }}

If you have any questions, please contact us.

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #FF5722; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #FF5722; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Booking Marked as Absent</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your booking has been marked as absent.</p>
            
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
                <div class="detail-row"><span class="label">Start Time:</span> {{ start_time }}</div>
                <div class="detail-row"><span class="label">End Time:</span> {{ end_time }}</div>
            </div>
            
            <p><strong>Note:</strong> {{ comment }}</p>
            
            <p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            <p>If you have any questions, please contact us.</p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when a booking is marked as absent',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ comment }}, {{ link }}',
            'is_active': True,
        },
        # Booking Status Changed Email
        {
            'name': 'Booking Status Changed Email',
            'code': 'booking_status_changed_email',
            'communication_type': 'email',
            'subject': 'Booking Status Updated - {{ equipment_name }} (Booking #{{ booking_id }})',
            'body_text': '''Hello {{ user_name }},

Your booking status has been updated.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Previous Status: {{ previous_status }}
- New Status: {{ new_status }}

{{ comment }}

You can view your booking details at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #9C27B0; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #9C27B0; }
        .status-change { background-color: #E1BEE7; padding: 15px; margin: 15px 0; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Booking Status Updated</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your booking status has been updated.</p>
            
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
            </div>
            
            <div class="status-change">
                <div class="detail-row"><span class="label">Previous Status:</span> {{ previous_status }}</div>
                <div class="detail-row"><span class="label">New Status:</span> {{ new_status }}</div>
            </div>
            
            <p><strong>Note:</strong> {{ comment }}</p>
            
            <p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when a booking status changes',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ previous_status }}, {{ new_status }}, {{ comment }}, {{ link }}',
            'is_active': True,
        },
        # Booking Comment Email
        {
            'name': 'Booking Comment Email',
            'code': 'booking_comment_email',
            'communication_type': 'email',
            'subject': 'New Comment on Booking - {{ equipment_name }} (Booking #{{ booking_id }})',
            'body_text': '''Hello {{ user_name }},

A new comment has been added to your booking.

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})

Comment:
{{ comment }}

You can view your booking details at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #607D8B; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .booking-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #607D8B; }
        .comment-box { background-color: #FFF9C4; padding: 15px; margin: 15px 0; border-left: 4px solid #FBC02D; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>New Comment on Booking</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>A new comment has been added to your booking.</p>
            
            <div class="booking-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
            </div>
            
            <div class="comment-box">
                <h3>Comment</h3>
                <p>{{ comment }}</p>
            </div>
            
            <p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when a comment is added to a booking',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ comment }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Credit Email
        {
            'name': 'Wallet Credit Email',
            'code': 'wallet_credit_email',
            'communication_type': 'email',
            'subject': 'Wallet Credited - ₹{{ amount }}',
            'body_text': '''Hello {{ user_name }},

Your wallet has been credited.

Transaction Details:
- Amount: ₹{{ amount }}
- Description: {{ description }}
- Department: {{ department_name }} ({{ department_code }})
- New Balance: ₹{{ balance }}
- Transaction Date: {{ transaction_date }}

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})

You can view your wallet transactions at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #4CAF50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .transaction-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; }
        .amount-box { background-color: #E8F5E9; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; font-size: 18px; font-weight: bold; text-align: center; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Wallet Credited</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your wallet has been credited.</p>
            
            <div class="amount-box">
                Amount Credited: ₹{{ amount }}
            </div>
            
            <div class="transaction-details">
                <h3>Transaction Details</h3>
                <div class="detail-row"><span class="label">Description:</span> {{ description }}</div>
                <div class="detail-row"><span class="label">Department:</span> {{ department_name }}{% if department_code %} ({{ department_code }}){% endif %}</div>
                <div class="detail-row"><span class="label">New Balance:</span> ₹{{ balance }}</div>
                <div class="detail-row"><span class="label">Transaction Date:</span> {{ transaction_date }}</div>
            </div>
            
            <div class="transaction-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
            </div>
            
            <p>You can view your wallet transactions at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when wallet is credited (refund or recharge)',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ amount }}, {{ description }}, {{ balance }}, {{ department_name }}, {{ department_code }}, {{ transaction_date }}, {{ is_booking_related }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Debit Email
        {
            'name': 'Wallet Debit Email',
            'code': 'wallet_debit_email',
            'communication_type': 'email',
            'subject': 'Wallet Debited - ₹{{ amount }}',
            'body_text': '''Hello {{ user_name }},

Amount has been debited from your wallet.

Transaction Details:
- Amount: ₹{{ amount }}
- Description: {{ description }}
- Department: {{ department_name }} ({{ department_code }})
- New Balance: ₹{{ balance }}
- Transaction Date: {{ transaction_date }}

Booking Details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})

You can view your wallet transactions at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #f44336; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .transaction-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #f44336; }
        .amount-box { background-color: #FFEBEE; padding: 15px; margin: 15px 0; border-left: 4px solid #f44336; font-size: 18px; font-weight: bold; text-align: center; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Wallet Debited</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Amount has been debited from your wallet.</p>
            
            <div class="amount-box">
                Amount Debited: ₹{{ amount }}
            </div>
            
            <div class="transaction-details">
                <h3>Transaction Details</h3>
                <div class="detail-row"><span class="label">Description:</span> {{ description }}</div>
                <div class="detail-row"><span class="label">Department:</span> {{ department_name }}{% if department_code %} ({{ department_code }}){% endif %}</div>
                <div class="detail-row"><span class="label">New Balance:</span> ₹{{ balance }}</div>
                <div class="detail-row"><span class="label">Transaction Date:</span> {{ transaction_date }}</div>
            </div>
            
            <div class="transaction-details">
                <h3>Booking Details</h3>
                <div class="detail-row"><span class="label">Booking ID:</span> {{ booking_id }}</div>
                <div class="detail-row"><span class="label">Equipment:</span> {{ equipment_name }} ({{ equipment_code }})</div>
            </div>
            
            <p>You can view your wallet transactions at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when wallet is debited (booking payment)',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ amount }}, {{ description }}, {{ balance }}, {{ department_name }}, {{ department_code }}, {{ transaction_date }}, {{ is_booking_related }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Recharge Approved Email
        {
            'name': 'Wallet Recharge Approved Email',
            'code': 'wallet_recharge_approved_email',
            'communication_type': 'email',
            'subject': 'Wallet Recharge Approved - ₹{{ amount }}',
            'body_text': '''Hello {{ user_name }},

Your wallet recharge request has been approved!

Recharge Details:
- Request ID: {{ request_id }}
- Amount: ₹{{ amount }}
- Department: {{ department_name }} ({{ department_code }})
- New Balance: ₹{{ balance }}
- Request Date: {{ request_date }}
- Approved By: {{ approved_by_email }}
- Project Details: {{ project_details }}
- Response: {{ response_message }}

You can view your wallet transactions at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #4CAF50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .recharge-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; }
        .amount-box { background-color: #E8F5E9; padding: 15px; margin: 15px 0; border-left: 4px solid #4CAF50; font-size: 18px; font-weight: bold; text-align: center; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Wallet Recharge Approved!</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your wallet recharge request has been approved!</p>
            
            <div class="amount-box">
                Amount Approved: ₹{{ amount }}
            </div>
            
            <div class="recharge-details">
                <h3>Recharge Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Amount:</span> ₹{{ amount }}</div>
                <div class="detail-row"><span class="label">Department:</span> {{ department_name }} ({{ department_code }})</div>
                <div class="detail-row"><span class="label">New Balance:</span> ₹{{ balance }}</div>
                <div class="detail-row"><span class="label">Request Date:</span> {{ request_date }}</div>
                <div class="detail-row"><span class="label">Approved By:</span> {{ approved_by_email }}</div>
            </div>
            
            <p><strong>Project Details:</strong> {{ project_details }}</p>
            
            <p><strong>Response:</strong> {{ response_message }}</p>
            
            <p>You can view your wallet transactions at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when wallet recharge request is approved',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ amount }}, {{ balance }}, {{ request_id }}, {{ request_date }}, {{ project_details }}, {{ status }}, {{ response_message }}, {{ approved_by_email }}, {{ department_name }}, {{ department_code }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Recharge Rejected Email
        {
            'name': 'Wallet Recharge Rejected Email',
            'code': 'wallet_recharge_rejected_email',
            'communication_type': 'email',
            'subject': 'Wallet Recharge Rejected - ₹{{ amount }}',
            'body_text': '''Hello {{ user_name }},

Your wallet recharge request has been rejected.

Recharge Details:
- Request ID: {{ request_id }}
- Amount: ₹{{ amount }}
- Department: {{ department_name }} ({{ department_code }})
- Request Date: {{ request_date }}
- Project Details: {{ project_details }}
- Reason: {{ response_message }}

If you have any questions, please contact us.

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #f44336; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .recharge-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #f44336; }
        .rejection-box { background-color: #FFEBEE; padding: 15px; margin: 15px 0; border-left: 4px solid #f44336; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Wallet Recharge Rejected</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your wallet recharge request has been rejected.</p>
            
            <div class="recharge-details">
                <h3>Recharge Request Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Amount:</span> ₹{{ amount }}</div>
                <div class="detail-row"><span class="label">Department:</span> {{ department_name }} ({{ department_code }})</div>
                <div class="detail-row"><span class="label">Request Date:</span> {{ request_date }}</div>
            </div>
            
            <p><strong>Project Details:</strong> {{ project_details }}</p>
            
            <div class="rejection-box">
                <h3>Reason for Rejection</h3>
                <p>{{ response_message }}</p>
            </div>
            
            <p>If you have any questions, please contact us.</p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when wallet recharge request is rejected',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, {{ project_details }}, {{ status }}, {{ response_message }}, {{ department_name }}, {{ department_code }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Recharge Pending Email
        {
            'name': 'Wallet Recharge Pending Email',
            'code': 'wallet_recharge_pending_email',
            'communication_type': 'email',
            'subject': 'Wallet Recharge Request Submitted - ₹{{ amount }}',
            'body_text': '''Hello {{ user_name }},

Your wallet recharge request has been submitted and is pending approval.

Recharge Details:
- Request ID: {{ request_id }}
- Amount: ₹{{ amount }}
- Department: {{ department_name }} ({{ department_code }})
- Request Date: {{ request_date }}
- Project Details: {{ project_details }}

Your request is currently under review. You will be notified once a decision has been made.

You can view your recharge request status at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #FF9800; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .recharge-details { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #FF9800; }
        .pending-box { background-color: #FFF3E0; padding: 15px; margin: 15px 0; border-left: 4px solid #FF9800; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Recharge Request Submitted</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your wallet recharge request has been submitted and is pending approval.</p>
            
            <div class="recharge-details">
                <h3>Recharge Request Details</h3>
                <div class="detail-row"><span class="label">Request ID:</span> {{ request_id }}</div>
                <div class="detail-row"><span class="label">Amount:</span> ₹{{ amount }}</div>
                <div class="detail-row"><span class="label">Department:</span> {{ department_name }} ({{ department_code }})</div>
                <div class="detail-row"><span class="label">Request Date:</span> {{ request_date }}</div>
            </div>
            
            <p><strong>Project Details:</strong> {{ project_details }}</p>
            
            <div class="pending-box">
                <p><strong>Status:</strong> Pending Approval</p>
                <p>Your request is currently under review. You will be notified once a decision has been made.</p>
            </div>
            
            <p>You can view your recharge request status at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when wallet recharge request is created/pending',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ amount }}, {{ request_id }}, {{ request_date }}, {{ project_details }}, {{ status }}, {{ department_name }}, {{ department_code }}, {{ link }}',
            'is_active': True,
        },
        # Wallet Low Balance Email
        {
            'name': 'Wallet Low Balance Email',
            'code': 'wallet_low_balance_email',
            'communication_type': 'email',
            'subject': 'Low Wallet Balance Alert - ₹{{ balance }}',
            'body_text': '''Hello {{ user_name }},

Your wallet balance is running low!

Current Balance: ₹{{ balance }}
Threshold: ₹{{ threshold }}

Your wallet balance has fallen below the threshold amount. Please recharge your wallet to continue making bookings.

You can recharge your wallet at: {{ link }}

Thank you for using IIC Booking System!''',
            'body_html': '''<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
        .container { max-width: 600px; margin: 0 auto; padding: 20px; }
        .header { background-color: #FF5722; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }
        .content { background-color: #f9f9f9; padding: 20px; border-radius: 0 0 5px 5px; }
        .balance-box { background-color: #FFEBEE; padding: 20px; margin: 15px 0; border-left: 4px solid #FF5722; text-align: center; }
        .balance-amount { font-size: 24px; font-weight: bold; color: #f44336; margin: 10px 0; }
        .threshold-info { background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #FF9800; }
        .detail-row { margin: 10px 0; }
        .label { font-weight: bold; color: #555; }
        .footer { text-align: center; margin-top: 20px; color: #777; font-size: 12px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Low Wallet Balance Alert</h2>
        </div>
        <div class="content">
            <p>Hello {{ user_name }},</p>
            <p>Your wallet balance is running low!</p>
            
            <div class="balance-box">
                <div class="balance-amount">₹{{ balance }}</div>
                <p>Current Balance</p>
            </div>
            
            <div class="threshold-info">
                <div class="detail-row"><span class="label">Threshold:</span> ₹{{ threshold }}</div>
                <p>Your wallet balance has fallen below the threshold amount. Please recharge your wallet to continue making bookings.</p>
            </div>
            
            <p>You can recharge your wallet at: <a href="{{ link }}">{{ link }}</a></p>
            
            <div class="footer">
                <p>Thank you for using IIC Booking System!</p>
            </div>
        </div>
    </div>
</body>
</html>''',
            'description': 'Email sent when wallet balance falls below threshold',
            'variable_help': 'Available variables: {{ user_name }}, {{ user_email }}, {{ balance }}, {{ threshold }}, {{ link }}',
            'is_active': True,
        },
    ]
    
    for template_data in templates:
        # Check if template already exists to avoid duplicates
        if not CommunicationTemplate.objects.filter(code=template_data['code']).exists():
            CommunicationTemplate.objects.create(**template_data)


def remove_booking_wallet_templates(apps, schema_editor):
    """Remove booking and wallet email templates (reverse migration)."""
    CommunicationTemplate = apps.get_model('communication', 'CommunicationTemplate')
    
    template_codes = [
        'booking_created_email',
        'booking_cancelled_email',
        'booking_rescheduled_email',
        'booking_completed_email',
        'booking_refunded_email',
        'booking_absent_email',
        'booking_status_changed_email',
        'booking_comment_email',
        'wallet_credit_email',
        'wallet_debit_email',
        'wallet_recharge_approved_email',
        'wallet_recharge_rejected_email',
        'wallet_recharge_pending_email',
        'wallet_low_balance_email',
    ]
    
    CommunicationTemplate.objects.filter(code__in=template_codes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('communication', '0005_add_expiry_date_to_notice'),
    ]

    operations = [
        migrations.RunPython(create_booking_wallet_templates, remove_booking_wallet_templates),
    ]

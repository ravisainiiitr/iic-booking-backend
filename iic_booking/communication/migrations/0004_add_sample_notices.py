# Generated manually to add sample notices

from django.db import migrations


def create_sample_notices(apps, schema_editor):
    """Create sample notices for the notice board."""
    Notice = apps.get_model('communication', 'Notice')
    
    sample_notices = [
        {
            'title': 'Equipment Maintenance Scheduled',
            'description': 'XRD will be under maintenance from Dec 25-26, 2024. Please plan your bookings accordingly.',
            'content': 'The Powder X-Ray Diffractometer (XRD) will be undergoing scheduled maintenance from December 25-26, 2024. All bookings for this period have been cancelled. We apologize for any inconvenience.',
            'notice_type': 'warning',
            'is_active': True,
            'priority': 2,
        },
        {
            'title': 'New Equipment Available',
            'description': 'MALDI-TOF/TOF MS is now available for booking. Advanced mass spectrometry capabilities now accessible.',
            'content': 'We are pleased to announce that the Matrix-Assisted Laser Desorption/Ionization Time-of-Flight Mass Spectrometry (MALDI-TOF/TOF MS) system is now operational and available for booking. This advanced instrument provides high-resolution mass spectrometry capabilities for protein and peptide analysis.',
            'notice_type': 'info',
            'is_active': True,
            'priority': 1,
        },
        {
            'title': 'Urgent: TEM Downtime',
            'description': 'TEM is temporarily unavailable due to technical issues. Expected resolution: 2-3 business days.',
            'content': 'The Transmission Electron Microscope (TEM) is currently experiencing technical difficulties and is temporarily unavailable for bookings. Our technical team is working to resolve the issue. Expected resolution time: 2-3 business days. All affected bookings will be notified and rescheduled.',
            'notice_type': 'urgent',
            'is_active': True,
            'priority': 3,
        },
        {
            'title': 'Holiday Schedule',
            'description': 'Limited operations during Dec 24-26. Plan bookings accordingly.',
            'content': 'Please note that the Institute Instrumentation Centre will have limited operations during the holiday period (December 24-26, 2024). Some equipment may have reduced availability. Please plan your bookings in advance and contact the facility if you have urgent requirements.',
            'notice_type': 'info',
            'is_active': True,
            'priority': 1,
        },
        {
            'title': 'Training Session',
            'description': 'FE-SEM training session scheduled for internal users on Dec 28, 2024.',
            'content': 'A training session for the Field Emission Scanning Electron Microscope (FE-SEM Gemini 560) has been scheduled for December 28, 2024, from 10:00 AM to 2:00 PM. This session is open to all internal users. Please register in advance as seats are limited.',
            'notice_type': 'info',
            'is_active': True,
            'priority': 1,
        },
        {
            'title': 'System Upgrade Complete',
            'description': 'Booking system has been upgraded with new features. Enhanced user experience now available.',
            'content': 'We have successfully upgraded our booking system with new features including improved slot selection, real-time availability updates, and enhanced notification system. Please explore the new features and provide feedback.',
            'notice_type': 'info',
            'is_active': True,
            'priority': 0,
        },
    ]
    
    for notice_data in sample_notices:
        # Check if notice already exists to avoid duplicates
        if not Notice.objects.filter(title=notice_data['title']).exists():
            Notice.objects.create(**notice_data)


def remove_sample_notices(apps, schema_editor):
    """Remove sample notices (reverse migration)."""
    Notice = apps.get_model('communication', 'Notice')
    
    sample_titles = [
        'Equipment Maintenance Scheduled',
        'New Equipment Available',
        'Urgent: TEM Downtime',
        'Holiday Schedule',
        'Training Session',
        'System Upgrade Complete',
    ]
    
    Notice.objects.filter(title__in=sample_titles).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('communication', '0003_rename_communicati_is_acti_123456_idx_communicati_is_acti_2447e0_idx_and_more'),
    ]

    operations = [
        migrations.RunPython(create_sample_notices, remove_sample_notices),
    ]

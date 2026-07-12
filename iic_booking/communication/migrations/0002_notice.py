# Generated manually for Notice model

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('communication', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Notice',
            fields=[
                ('notice_id', models.AutoField(primary_key=True, serialize=False)),
                ('title', models.CharField(help_text='Title of the notice', max_length=255, verbose_name='Title')),
                ('description', models.TextField(help_text='Short description of the notice', verbose_name='Description')),
                ('content', models.TextField(blank=True, help_text='Full content of the notice (optional)', null=True, verbose_name='Content')),
                ('notice_type', models.CharField(choices=[('info', 'Info'), ('warning', 'Warning'), ('urgent', 'Urgent')], db_index=True, default='info', help_text='Type of notice', max_length=20, verbose_name='Notice Type')),
                ('is_active', models.BooleanField(db_index=True, default=True, help_text='Whether the notice is active and visible', verbose_name='Is Active')),
                ('priority', models.IntegerField(default=0, help_text='Priority for sorting (higher = more important)', verbose_name='Priority')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Created at')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Updated at')),
                ('created_by', models.ForeignKey(blank=True, help_text='User who created this notice', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='notices_created', to=settings.AUTH_USER_MODEL, verbose_name='Created By')),
            ],
            options={
                'verbose_name': 'Notice',
                'verbose_name_plural': 'Notices',
                'ordering': ['-priority', '-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='notice',
            index=models.Index(fields=['is_active', 'created_at'], name='communicati_is_acti_123456_idx'),
        ),
        migrations.AddIndex(
            model_name='notice',
            index=models.Index(fields=['notice_type', 'is_active'], name='communicati_notice__789012_idx'),
        ),
    ]

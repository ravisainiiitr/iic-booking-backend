# Compatibility stub for production recoveries.
# Some hosts previously had a leftover 0016_agent_installer_release that depended
# on this node. Keep an empty 0015 so Django can load even if that leftover
# file is present in an old image layer.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0014_analysis_workflows"),
    ]

    operations = []

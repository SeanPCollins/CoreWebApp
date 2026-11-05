from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('registry', '0028_rename_resolutionrun_to_datagatheringrun'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='webfinding',
            index=models.Index(
                fields=['updated_at', 'id'],
                name='webfinding_updated_pk_idx',
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0026_resolutionrun_max_tasks'),
    ]

    operations = [
        migrations.AddField(
            model_name='resolutionrun',
            name='force_chemspider_small_batches',
            field=models.BooleanField(
                default=False,
                help_text='Allow ChemSpider detail requests with fewer than 50 IDs.',
            ),
        ),
    ]

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('registry', '0027_resolutionrun_force_chemspider_small_batches'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='ResolutionRun',
            new_name='DataGatheringRun',
        ),
        migrations.AlterModelTable(
            name='datagatheringrun',
            table='core_data_gathering_run',
        ),
    ]

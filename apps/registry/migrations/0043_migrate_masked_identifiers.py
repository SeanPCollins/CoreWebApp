from django.db import migrations
from django.db.models import Q


def migrate_masked_identifiers(apps, schema_editor):
    database = schema_editor.connection.alias
    Identifier = apps.get_model('registry', 'Identifier')
    RawSubstanceInput = apps.get_model('registry', 'RawSubstanceInput')
    SearchTask = apps.get_model('registry', 'SearchTask')
    SearchTerm = apps.get_model('registry', 'SearchTerm')

    masked_terms = SearchTerm.objects.using(database).filter(
        Q(term_type__iexact='Masked ID')
        | Q(term_type__iexact='Masked_ID')
        | Q(term_type__iexact='Masked-ID')
    )
    masked_term_ids = masked_terms.values('pk')
    masked_inputs = RawSubstanceInput.objects.using(database).filter(
        Q(search_term_id__in=masked_term_ids)
        | Q(input_type__iexact='Masked ID')
        | Q(input_type__iexact='Masked_ID')
        | Q(input_type__iexact='Masked-ID')
    )

    def ensure_identifiers(values):
        values = set(values)
        if any(len(value) > 255 for value in values):
            raise ValueError(
                'A masked identifier exceeds the 255-character identifier '
                'limit; resolve it before applying this migration.'
            )

        existing = {
            value
            for value in Identifier.objects.using(database).filter(
                identifier_type='MASKED_ID',
                identifier_value__in=values,
            ).values_list('identifier_value', flat=True)
        }
        missing = values - existing
        if missing:
            Identifier.objects.using(database).bulk_create(
                [
                    Identifier(
                        identifier_type='MASKED_ID',
                        identifier_value=value,
                    )
                    for value in missing
                ],
                ignore_conflicts=True,
                batch_size=2000,
            )
        return {
            value: identifier_id
            for value, identifier_id in Identifier.objects.using(database).filter(
                identifier_type='MASKED_ID',
                identifier_value__in=values,
            ).values_list('identifier_value', 'pk')
        }

    masked_term_values = masked_terms.order_by('pk').values_list(
        'term_value',
        flat=True,
    )
    for offset in range(0, masked_terms.count(), 2000):
        ensure_identifiers(masked_term_values[offset:offset + 2000])

    while True:
        input_batch = list(masked_inputs.order_by('pk')[:2000])
        if not input_batch:
            break
        identifier_ids = ensure_identifiers(
            item.input_value for item in input_batch
        )
        for item in input_batch:
            item.identifier_id = identifier_ids[item.input_value]
            item.search_term_id = None
        RawSubstanceInput.objects.using(database).bulk_update(
            input_batch,
            ['identifier', 'search_term'],
            batch_size=2000,
        )
        masked_inputs = masked_inputs.filter(pk__gt=input_batch[-1].pk)

    SearchTask.objects.using(database).filter(
        search_term__in=masked_term_ids,
    ).delete()
    masked_terms.delete()


class Migration(migrations.Migration):
    dependencies = [
        ('registry', '0042_rawsubstanceinput_identifier_and_more'),
    ]

    operations = [
        migrations.RunPython(
            migrate_masked_identifiers,
            reverse_code=migrations.RunPython.noop,
        ),
    ]

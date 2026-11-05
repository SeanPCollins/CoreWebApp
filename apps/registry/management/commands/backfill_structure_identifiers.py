from django.core.management.base import BaseCommand

from apps.registry.services.identifier_linking import link_verified_identifiers


class Command(BaseCommand):
    help = (
        'Backfill identifiers from supporting WebFinding evidence. '
        'Runs as a dry run unless --apply is supplied.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Create the verified identifier links.',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        summary = link_verified_identifiers(apply=apply)
        if apply:
            self.stdout.write(self.style.SUCCESS(
                f"Created {summary['created']} identifier link(s); "
                f"{summary['already_linked']} already existed; "
                f"{summary['multi_structure_values']} identifier value(s) "
                'are supported for multiple structures.'
            ))
        else:
            self.stdout.write(
                f"Dry run: {summary['would_create']} identifier link(s) "
                f"would be created; {summary['already_linked']} already "
                f"exist; {summary['multi_structure_values']} identifier "
                'value(s) have supporting evidence for multiple structures. '
                'Use --apply to write links.'
            )

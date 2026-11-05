from django.core.management.base import BaseCommand
from apps.registry.services.cactus import dry_run_cactus_search

class Command(BaseCommand):
    help = 'Runs a dry-run test of the CACTUS resolver on existing search terms without saving.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=5, help='Number of terms to test')

    def handle(self, *args, **options):
        limit = options['limit']
        dry_run_cactus_search(limit=limit)

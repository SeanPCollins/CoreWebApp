from django.core.management.base import BaseCommand
from apps.registry.services.cactus import run_batch_cactus_resolution

class Command(BaseCommand):
    help = 'Resolves pending search terms or re-checks existing terms using external chemical resolvers.'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=50, help='Number of terms to process')
        parser.add_argument('--provider', type=str, default='cactus', help='Resolver provider to use (default: cactus)')
        parser.add_argument('--recheck', action='store_true', help='Use weighted age-based random choice to recheck old terms')

    def handle(self, *args, **options):
        provider = options['provider'].lower()
        batch_size = options['batch_size']
        recheck = options['recheck']

        if provider == 'cactus':
            self.stdout.write(self.style.SUCCESS(f"Running CACTUS resolution batch (Batch Size: {batch_size}, Recheck: {recheck})..."))
            run_batch_cactus_resolution(batch_size=batch_size, force_recheck=recheck)
        else:
            self.stdout.write(self.style.ERROR(f"Unknown provider '{provider}'. Currently supported: 'cactus'."))

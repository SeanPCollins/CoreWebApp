from django.core.management.base import BaseCommand
import os
from apps.registry.services.parser import parse_and_stage_csv

class Command(BaseCommand):
    help = 'Ingest a chemical CSV file directly from the command line'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str, help='Absolute or relative path to the CSV file')

    def handle(self, *args, **options):
        file_path = options['file_path']
        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f"File not found: {file_path}"))
            return

        self.stdout.write(self.style.SUCCESS(f"Found file at {file_path}. Ready for programmatic batch processing integration."))

import os
import subprocess
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

class Command(BaseCommand):
    help = 'Restores the PostgreSQL database back to the pre-scraping seed snapshot.'

    def handle(self, *args, **options):
        db_settings = settings.DATABASES['default']
        
        if 'postgresql' not in db_settings['ENGINE']:
            raise CommandError("This command is configured for PostgreSQL databases.")

        db_name = db_settings['NAME']
        db_user = db_settings['USER']
        db_host = db_settings['HOST'] or 'localhost'
        db_port = db_settings['PORT'] or '5432'
        db_password = db_settings['PASSWORD']

        seed_path = os.path.join(settings.BASE_DIR, 'archive', 'db_seed.dump')

        if not os.path.exists(seed_path):
            raise CommandError(f"Seed snapshot not found at {seed_path}. Create it first using pg_dump.")

        # Set password in environment so pg_restore doesn't prompt for it interactively
        env = os.environ.copy()
        if db_password:
            env['PGPASSWORD'] = db_password

        # Use pg_restore to overwrite the existing database cleanly (-c drops objects first, or --clean)
        cmd = [
            'pg_restore',
            '-h', db_host,
            '-p', str(db_port),
            '-U', db_user,
            '-d', db_name,
            '--clean',
            '--if-exists',
            seed_path
        ]

        self.stdout.write("Restoring database from seed snapshot...")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)

        if result.returncode != 0:
            # Note: pg_restore often throws minor warnings (exit code 1) for dropping non-existent objects, 
            # but if it fails critically, print the error.
            if "error" in result.stderr.lower() and "warning" not in result.stderr.lower():
                raise CommandError(f"Postgres restore failed:\n{result.stderr}")

        self.stdout.write(self.style.SUCCESS("Successfully reverted PostgreSQL database back to snapshot!"))

import os
import sys
import django

# 1. Setup Django Environment (Ensure os.environ is used, not os.path)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.db import transaction
from apps.registry.models import WebSource, SearchTerm, SearchTask, WebFinding, ResolutionTaskLog
from apps.registry.services.providers.pubchem import execute_batch

def test_normalization_logic():
    print("[*] Setting up CID Normalization Test...")

    # Wrap everything in an atomic transaction that rolls back at the end
    with transaction.atomic():
        # Get or create a test PubChem WebSource
        web_source, _ = WebSource.objects.get_or_create(
            provider_code='pubchem',
            defaults={
                'name': 'PubChem API (Test)',
                'base_url': 'https://pubchem.ncbi.nlm.nih.gov',
                'is_active': True
            }
        )

        # Create two distinct search terms representing different identifiers for Benzene
        term_1, _ = SearchTerm.objects.get_or_create(
            term_value='benzene',
            defaults={'term_type': 'Name'}
        )
        term_2, _ = SearchTerm.objects.get_or_create(
            term_value='71-43-2',  # CAS for benzene, will also resolve to CID 241
            defaults={'term_type': 'CAS'}
        )

        # Create two separate search tasks for these different terms under PubChem
        task_1 = SearchTask.objects.create(
            search_term=term_1,
            web_source=web_source,
            target_representation='default',
            status=SearchTask.TaskStatus.PENDING
        )
        task_2 = SearchTask.objects.create(
            search_term=term_2,
            web_source=web_source,
            target_representation='default',
            status=SearchTask.TaskStatus.PENDING
        )

        # Create a mock task log to track metrics
        task_log = ResolutionTaskLog.objects.create(
            provider='pubchem-test',
            status='RUNNING',
            total_items=2
        )

        print(f"[*] Created 2 separate tasks:")
        print(f"    - Task 1: '{term_1.term_value}' (ID: {task_1.id})")
        print(f"    - Task 2: '{term_2.term_value}' (ID: {task_2.id})")
        print("[*] Executing batch resolution...")

        # Run the batch processor
        execute_batch([task_1, task_2], task_log)

        print("\n" + "=" * 50)
        print(" NORMALIZATION TEST RESULTS:")
        print("=" * 50)
        
        # Check how many WebFinding objects were created in total
        total_findings = WebFinding.objects.count()
        print(f" Total WebFinding Records in DB: {total_findings} (Expected: Exactly 1 Master Record)")

        # Verify task statuses
        task_1.refresh_from_db()
        task_2.refresh_from_db()
        print(f" Task 1 Status: {task_1.status} | Error Message: {task_1.error_message}")
        print(f" Task 2 Status: {task_2.status} | Error Message: {task_2.error_message}")

        # Verify Master CID task was created
        master_term = SearchTerm.objects.filter(term_value='CID:241').first()
        if master_term:
            print(f" [+] Master CID SearchTerm 'CID:241' successfully auto-created!")
        else:
            print(f" [-] Master CID SearchTerm 'CID:241' was not found.")

        print("=" * 50)
        
        # Force rollback so no permanent data is written during testing
        transaction.set_rollback(True)
        print("[+] Test completed and rolled back cleanly. Zero changes persisted to database.")

if __name__ == "__main__":
    test_normalization_logic()

from django.core.management.base import BaseCommand
from apps.registry.models import SearchTask, SearchTerm, ProviderTaskLog
from apps.registry.services.task_generator import propagate_all_pending_terms

class Command(BaseCommand):
    help = 'Resets the pipeline: wipes all search tasks/findings, drops raw CID tasks/terms, and reinitializes fresh variant tasks.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("[*] Starting pipeline reset..."))

        # 1. Delete all SearchTasks (Cascades to WebFindings automatically)
        task_count, _ = SearchTask.objects.all().delete()
        self.stdout.write(f"[*] Deleted {task_count} search tasks and their findings.")

        # 2. Drop raw CID SearchTerms so they never enter the search queue
        cid_terms_qs = SearchTerm.objects.filter(term_type='PubChem_CID') | \
                       SearchTerm.objects.filter(term_value__startswith='CID:') | \
                       SearchTerm.objects.filter(term_value__regex=r'^\d+$')
        
        cid_term_count = cid_terms_qs.count()
        cid_terms_qs.delete()
        self.stdout.write(f"[*] Purged {cid_term_count} raw CID search terms from the registry.")

        # 3. Clear old execution logs
        log_count, _ = ProviderTaskLog.objects.all().delete()
        self.stdout.write(f"[*] Cleared {log_count} old execution task logs.")

        # 4. Reinitialize fresh PENDING variant search tasks (Names, CAS, SMILES, InChIKeys)
        created_count = propagate_all_pending_terms()
        self.stdout.write(self.style.SUCCESS(f"[+] Successfully reinitialized {created_count} fresh PENDING search tasks (CIDs excluded)!"))

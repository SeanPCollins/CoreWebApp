import urllib.parse
from django.utils import timezone
from apps.registry.services.base import BaseProvider

PROVIDER_CONFIG = {
    "accepts_cids": False,
    "accepted_term_types": ["NAME", "CAS_RN", "SMILES", "INCHI", "INCHIKEY"],
    "representations": ["stdinchikey", "smiles", "iupac_name"]
}

class CactusProvider(BaseProvider):
    provider_code = "cactus"
    min_request_interval = 1.0  # CACTUS polite rate limit

    def execute_batch(self, tasks, task_log, *args, **kwargs):
        for task in tasks:
            if self.cancellation_requested(task_log):
                break

            self.save_progress_checkpoint(task_log)
            task_log.processed_items += 1
            task.last_run_at = timezone.now()

            term_value = task.search_term.term_value if task.search_term else ""
            source = task.web_source
            rep = task.target_representation

            base_url = source.base_url or "https://cactus.nci.nih.gov/chemical/structure"
            safe_term = urllib.parse.quote(term_value, safe='')
            url_called = f"{base_url.rstrip('/')}/{safe_term}/{rep}"

            try:
                self.polite_throttle()
                response = self.session.get(url_called, timeout=10)

                if response.status_code == 200:
                    payload = response.text.strip()
                    self.handle_task_success(task, url_called, payload, "Retrieved successfully from CACTUS.", task_log)
                elif response.status_code == 404:
                    self.handle_task_not_found(task, url_called, "404 Not Found", task_log)
                else:
                    self.handle_task_error(task, url_called, f"HTTP Error {response.status_code}", task_log)

            except Exception as e:
                self.handle_task_error(task, url_called, str(e), task_log)

        task_log.save(
            update_fields=[
                'processed_items',
                'success_count',
                'not_found_count',
                'error_count',
            ]
        )

# Expose standard execution function expected by the dispatcher runner
def execute_batch(tasks, task_log, *args, **kwargs):
    provider = CactusProvider()
    provider.execute_batch(tasks, task_log, *args, **kwargs)

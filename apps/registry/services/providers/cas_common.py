import json
import urllib.parse
from django.conf import settings
from django.utils import timezone
from apps.registry.models import WebFinding, SearchTask, SearchTerm
from apps.registry.services.base import BaseProvider

PROVIDER_CONFIG = {
    "accepts_cids": False,
    "accepted_term_types": ["NAME", "CAS", "SMILES", "INCHI", "INCHIKEY"],
    "representations": ["search", "detail"]
}

class CasCommonProvider(BaseProvider):
    provider_code = "cas_common"
    min_request_interval = 1.5
    default_retry_after_seconds = 15 * 60

    def get_default_headers(self):
        headers = super().get_default_headers()
        headers['accept'] = 'application/json'
        headers['x-origin'] = 'https://commonchemistry.cas.org/api-overview'
        
        api_key = getattr(settings, 'CAS_COMMON_API_KEY', None)
        if api_key:
            headers['X-API-KEY'] = api_key
        return headers

    def execute_batch(self, tasks, task_log, *args, **kwargs):
        for task in tasks:
            if self.cancellation_requested(task_log):
                break

            task_log.processed_items += 1
            task.run_count += 1
            task.last_run_at = timezone.now()

            # Route execution based on whether this is a search or detail task
            representation = task.target_representation or 'search'

            if representation == 'detail':
                self._execute_detail_task(task, task_log)
            else:
                self._execute_search_task(task, task_log)

            if task_log.processed_items % 5 == 0:
                task_log.save(update_fields=['processed_items', 'success_count', 'not_found_count', 'error_count'])

        task_log.save(update_fields=['success_count', 'not_found_count', 'error_count', 'processed_items'])

    def _execute_search_task(self, task, task_log):
        """Phase 1: Queries /direct-api/search to find the canonical CAS RN."""
        term_value = task.search_term.term_value.strip() if task.search_term else ""
        if not term_value:
            self.handle_task_not_found(task, "", "Empty search term.", task_log)
            return

        search_url = ""
        try:
            safe_query = urllib.parse.quote(term_value)
            search_url = f"https://commonchemistry.cas.org/direct-api/search?q={safe_query}"

            self.polite_throttle()
            response = self.session.get(search_url, timeout=10)

            if response.status_code in (401, 403):
                self.handle_task_not_found(task, search_url, "CAS Direct API Authentication failed. Check API key.", task_log)
                return
            elif response.status_code in (404, 500):
                self.handle_task_not_found(task, search_url, f"CAS API returned status {response.status_code}.", task_log)
                return

            response.raise_for_status()
            data = response.json()

            results = data.get('results', [])
            if not results:
                self.handle_task_not_found(task, search_url, "No matching substance found in CAS Common Chemistry.", task_log)
                return

            match = results[0]

            # Drop the heavy SVG images payload completely; provider batches do not use it.
            match.pop('images', None)

            cas_rn = match.get('rn') or match.get('casRN')
            substance_name = match.get('name')

            if not cas_rn:
                self.handle_task_not_found(task, search_url, "Match returned without a valid CAS RN (rn).", task_log)
                return

            # Save Search Finding
            self.handle_task_success(
                task, search_url, json.dumps(match), 
                f"Resolved to canonical CAS RN {cas_rn} ({substance_name})", task_log
            )

            # AFTER successful search, create/enqueue the dedicated CAS Detail task
            self._ensure_and_queue_detail_task(cas_rn, task.web_source)

        except Exception as e:
            self.logger.exception(f"Error processing CAS search task for '{term_value}': {e}")
            self.handle_task_error(task, search_url, str(e), task_log)

    def _execute_detail_task(self, task, task_log):
        """Phase 2: Fetches comprehensive properties and synonyms using the canonical CAS RN."""
        cas_rn = task.search_term.term_value.strip() if task.search_term else ""
        detail_url = f"https://commonchemistry.cas.org/direct-api/detail?cas_rn={cas_rn}"

        try:
            self.polite_throttle()
            res = self.session.get(detail_url, timeout=15)
            res.raise_for_status()
            payload = res.json()

            # Strip only the massive graphic rendering markup; keep uri and propertyCitations!
            payload.pop('images', None)
            payload.pop('hasMolfile', None)

            self.handle_task_success(
                task, detail_url, json.dumps(payload), 
                "CAS Detail record retrieved successfully.", task_log
            )
        except Exception as e:
            self.logger.exception(f"Error fetching CAS detail for {cas_rn}: {e}")
            self.handle_task_error(task, detail_url, str(e), task_log)

    def _ensure_and_queue_detail_task(self, cas_rn, web_source):
        """Creates a dedicated detail SearchTask for the verified CAS RN if it doesn't already exist."""
        search_term, _ = SearchTerm.objects.get_or_create(
            term_value=cas_rn,
            defaults={'term_type': 'CAS', 'frequency_count': 1}
        )
        
        detail_task, _ = SearchTask.objects.get_or_create(
            search_term=search_term,
            web_source=web_source,
            target_representation='detail',
            defaults={
                'status': SearchTask.TaskStatus.PENDING,
                'is_completed': False
            }
        )
        return detail_task

def execute_batch(tasks, task_log, *args, **kwargs):
    provider = CasCommonProvider()
    provider.execute_batch(tasks, task_log, *args, **kwargs)

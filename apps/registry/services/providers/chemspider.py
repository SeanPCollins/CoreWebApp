import json
import time
from django.conf import settings
from django.utils import timezone
from apps.registry.models import SearchTask, SearchTerm, WebSource
from apps.registry.services.base import BaseProvider

from chemspipy import ChemSpider
from openbabel import openbabel as ob
from openbabel import pybel

# Silence OpenBabel C++ core warnings
ob.obErrorLog.SetOutputLevel(0)

PROVIDER_CONFIG = {
    "accepts_cids": False,
    "accepted_term_types": ["NAME", "CAS", "INCHIKEY", "SMILES", "CHEMSPIDER_ID"],
    "representations": ["default", "details"]
}

class ChemSpiderProvider(BaseProvider):
    provider_code = "chemspider"
    min_request_interval = 0.5  # Respect API rate limits

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        api_key = getattr(settings, 'CHEMSPIDER_API_KEY', '')
        if not api_key:
            self.logger.warning("CHEMSPIDER_API_KEY is missing or empty in settings.")
        self.cs = ChemSpider(api_key)

    def _spawn_chemspider_id_tasks(self, csids, current_source):
        """
        Creates pending SearchTerms and SearchTasks for discovered ChemSpider IDs (CSIDs).
        """
        spawned_count = 0
        for csid in csids:
            if not csid:
                continue
            cleaned_csid = str(csid).strip()

            term_obj, _ = SearchTerm.objects.get_or_create(
                term_value=cleaned_csid,
                defaults={'term_type': 'CHEMSPIDER_ID'}
            )

            _, created = SearchTask.objects.get_or_create(
                search_term=term_obj,
                web_source=current_source,
                target_representation='default',
                defaults={
                    'status': SearchTask.TaskStatus.PENDING,
                    'is_completed': False
                }
            )
            if created:
                spawned_count += 1
        return spawned_count

    def execute_batch(self, tasks, task_log, *args, force_small_batch=False, **kwargs):
        current_source = WebSource.objects.filter(provider_code=self.provider_code).first()

        discovery_tasks = []
        csid_tasks_map = {}  # {csid_int: task_object}

        for task in tasks:
            term_value = task.search_term.term_value.strip()
            term_type = (task.search_term.term_type or '').upper()

            if term_type == 'CHEMSPIDER_ID' or term_value.isdigit():
                try:
                    csid_tasks_map[int(term_value)] = task
                except ValueError:
                    discovery_tasks.append(task)
            else:
                discovery_tasks.append(task)

        # -------------------------------------------------------------------------
        # 1. PROCESS NATIVE CSID TASKS VIA BATCHING (With Quota Threshold Gating)
        # -------------------------------------------------------------------------
        csid_list = list(csid_tasks_map.keys())
        
        # Preserve the API quota by fetching details only when this selected batch
        # contains at least 50 ChemSpider IDs.
        MIN_BATCH_THRESHOLD = 50
        
        if csid_list and len(csid_list) < MIN_BATCH_THRESHOLD:
            if force_small_batch:
                self.logger.warning(
                    "Forcing ChemSpider detail lookup for %s CSID tasks, below the "
                    "%s-ID quota threshold.",
                    len(csid_list),
                    MIN_BATCH_THRESHOLD,
                )
        elif csid_list:
            chunk_size = 100
            optimal_fields = [
                'SMILES', 'Formula', 'MolecularWeight', 'CommonName', 'Mol2D',
                'AverageMass', 'MonoisotopicMass', 'NominalMass'
            ]

            for i in range(0, len(csid_list), chunk_size):
                if self.cancellation_requested(task_log):
                    break

                chunk = csid_list[i:i + chunk_size]

                try:
                    self.polite_throttle()
                    batch_results = self.cs.get_details_batch(chunk, fields=optimal_fields)

                    returned_ids = set()
                    returned_map = {}
                    
                    for record in batch_results:
                        csid = record.get('id') or record.get('recordId') or record.get('record_id') or record.get('identifier')
                        if csid:
                            returned_map[int(csid)] = record

                    for csid_int, record in returned_map.items():
                        if self.cancellation_requested(task_log):
                            break

                        returned_ids.add(csid_int)
                        task = csid_tasks_map.get(csid_int)
                        if not task:
                            continue

                        term_value = task.search_term.term_value.strip()
                        query_url = f"chemspider://search?chemspider_id={term_value}"

                        task.run_count += 1
                        task.last_run_at = timezone.now()
                        task.save(update_fields=['run_count', 'last_run_at'])
                        task_log.processed_items += 1

                        # Local OpenBabel generation (0 extra API calls)
                        mol2d_str = record.get('Mol2D') or record.get('mol2D')
                        inchi_ob, inchikey_ob = None, None
                        if mol2d_str:
                            try:
                                mol = pybel.readstring('mol', mol2d_str)
                                inchi_ob = mol.write('inchi').strip()
                                inchikey_ob = mol.write('inchikey').strip()
                            except Exception as conv_err:
                                self.logger.warning(f"OpenBabel conversion failed for ChemSpider record: {conv_err}")
                                inchi_ob, inchikey_ob = None, None

                        payload = {
                            "identifier_searched": term_value,
                            "chemspider_id": csid_int,
                            "common_name": record.get('CommonName') or record.get('commonName'),
                            "smiles": record.get('SMILES') or record.get('smiles'),
                            "formula": record.get('Formula') or record.get('formula'),
                            "molecular_weight": record.get('MolecularWeight') or record.get('molecularWeight'),
                            "average_mass": record.get('AverageMass') or record.get('averageMass'),
                            "monoisotopic_mass": record.get('MonoisotopicMass') or record.get('monoisotopicMass'),
                            "nominal_mass": record.get('NominalMass') or record.get('nominalMass'),
                            "inchi_openbabel": inchi_ob,
                            "inchikey_openbabel": inchikey_ob,
                            "structural_source": "openbabel_local_generation"
                        }

                        self.handle_task_success(
                            task, query_url, json.dumps(payload),
                            f"Successfully retrieved batch profile & computed local OpenBabel keys for ChemSpider ID {csid_int}.", task_log
                        )
                        self.save_progress_checkpoint(task_log)

                    # Mark missing IDs in this chunk as Not Found
                    missing_ids = set(chunk) - returned_ids
                    for missing_id in missing_ids:
                        if self.cancellation_requested(task_log):
                            break

                        task = csid_tasks_map.get(missing_id)
                        if task:
                            task.run_count += 1
                            task.last_run_at = timezone.now()
                            task.save(update_fields=['run_count', 'last_run_at'])
                            task_log.processed_items += 1
                            query_url = f"chemspider://search?chemspider_id={missing_id}"
                            self.handle_task_not_found(task, query_url, f"ChemSpider ID {missing_id} not found in batch.", task_log)
                            self.save_progress_checkpoint(task_log)

                except Exception as e:
                    if self.cancellation_requested(task_log):
                        break

                    for csid_int in chunk:
                        task = csid_tasks_map.get(csid_int)
                        if task:
                            query_url = f"chemspider://search?chemspider_id={csid_int}"
                            self.handle_task_error(task, query_url, str(e), task_log)
                            self.save_progress_checkpoint(task_log)

        # -------------------------------------------------------------------------
        # 2. PROCESS DISCOVERY TASKS (Name, CAS, InChIKey, SMILES)
        # -------------------------------------------------------------------------
        for task in discovery_tasks:
            if self.cancellation_requested(task_log):
                break

            term_value = task.search_term.term_value.strip()
            term_type = (task.search_term.term_type or '').upper()
            query_url = f"chemspider://search?{term_type.lower()}={term_value}"

            task.run_count += 1
            task.last_run_at = timezone.now()
            task.save(update_fields=['run_count', 'last_run_at'])
            task_log.processed_items += 1

            try:
                resolved_csids = set()
                self.polite_throttle()

                results = self.cs.search(term_value)
                if hasattr(results, 'wait'):
                    results.wait()

                for res in results:
                    if hasattr(res, 'csid') and res.csid:
                        resolved_csids.add(res.csid)
                    elif hasattr(res, 'record_id') and res.record_id:
                        resolved_csids.add(res.record_id)
                    elif isinstance(res, (int, str)) and str(res).isdigit():
                        resolved_csids.add(int(res))

                if not resolved_csids:
                    self.handle_task_not_found(task, query_url, f"No ChemSpider ID resolved for discovery term {term_value}.", task_log)
                    self.save_progress_checkpoint(task_log)
                    continue

                spawned_count = 0
                if current_source:
                    spawned_count = self._spawn_chemspider_id_tasks(resolved_csids, current_source)

                discovery_payload = {
                    "identifier_searched": term_value,
                    "term_type": term_type,
                    "resolved_chemspider_ids": list(resolved_csids),
                    "new_tasks_spawned": spawned_count
                }

                self.handle_task_success(
                    task, query_url, json.dumps(discovery_payload),
                    f"Resolved {term_value} to ChemSpider IDs: {list(resolved_csids)}", task_log
                )
                self.save_progress_checkpoint(task_log)

            except Exception as e:
                self.logger.exception(f"Error executing ChemSpider discovery task: {e}")
                self.handle_task_error(task, query_url, str(e), task_log)
                self.save_progress_checkpoint(task_log)

        task_log.save(update_fields=['success_count', 'not_found_count', 'error_count', 'processed_items'])

def execute_batch(tasks, task_log, *args, **kwargs):
    provider = ChemSpiderProvider()
    provider.execute_batch(tasks, task_log, *args, **kwargs)

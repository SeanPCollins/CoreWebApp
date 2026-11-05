from collections import defaultdict
import json
import time
from django.conf import settings
from django.utils import timezone
from apps.registry.models import WebFinding, SearchTask, SearchTerm, WebSource
from apps.registry.services.base import BaseProvider

from chembl_webresource_client.new_client import new_client
from chembl_webresource_client.settings import Settings

# Configure global client settings for fast failure and local caching
chem_settings = Settings.Instance()
chem_settings.TIMEOUT = 5
chem_settings.TOTAL_RETRIES = 2
chem_settings.CACHING = True
chem_settings.FAST_SAVE = True

PROVIDER_CONFIG = {
    "accepts_cids": False,
    "accepted_term_types": ["NAME", "CHEMBL_ID", "INCHIKEY", "SMILES"],
    "representations": ["default", "details"]
}

class ChEMBLProvider(BaseProvider):
    provider_code = "chembl"
    min_request_interval = 0.2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.molecule_client = new_client.molecule

    def _safe_fetch_records(self, client_query, label=""):
        """Evaluates ChEMBL lazy querysets utilizing client settings."""
        t_start = time.time()
        results = []

        try:
            self.polite_throttle()
            results = list(client_query)
        except Exception as net_err:
            raise net_err

        elapsed = time.time() - t_start
        return results

    def _spawn_chembl_id_tasks(self, chembl_ids, current_source):
        """
        Creates pending SearchTerms and SearchTasks for discovered ChEMBL IDs.
        """
        spawned_count = 0
        for cid in chembl_ids:
            if not cid:
                continue
            cleaned_cid = cid.upper().strip()

            term_obj, _ = SearchTerm.objects.get_or_create(
                term_value=cleaned_cid,
                defaults={'term_type': 'CHEMBL_ID'}
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

    def execute_batch(self, tasks, task_log, *args, **kwargs):
        current_source = WebSource.objects.filter(provider_code=self.provider_code).first()

        for task in tasks:
            if self.cancellation_requested(task_log):
                break

            term_value = task.search_term.term_value.strip()
            term_type = (task.search_term.term_type or '').upper()
            query_url = f"chembl://search?{term_type.lower()}={term_value}"

            task.run_count += 1
            task.last_run_at = timezone.now()
            task.save(update_fields=['run_count', 'last_run_at'])
            task_log.processed_items += 1

            # Strict Guard: Drop CAS-RNs immediately
            if term_type == 'CAS' or 'CAS' in term_type.upper():
                self.handle_task_not_found(
                    task, query_url,
                    "CAS-RNs are not supported by ChEMBL provider routing.",
                    task_log
                )
                self.save_progress_checkpoint(task_log)
                continue

            chem_start = time.time()

            try:
                discovered_chembl_ids = set()

                # CASE A: Native ChEMBL ID Task -> Fetch comprehensive structural & property payload
                if 'CHEMBL' in term_type or term_value.upper().startswith('CHEMBL'):
                    molecule_query = self.molecule_client.filter(molecule_chembl_id=term_value.upper())
                    mol_records = self._safe_fetch_records(molecule_query, label="ChEMBL Structural Record")

                    if not mol_records:
                        self.handle_task_not_found(task, query_url, f"ChEMBL ID {term_value} not found.", task_log)
                        self.save_progress_checkpoint(task_log)
                        continue

                    primary_molecule = mol_records[0]
                    chembl_id = primary_molecule.get("molecule_chembl_id")

                    # Clean structures (drop the bulky molfile)
                    structures = primary_molecule.get("molecule_structures", {})
                    if isinstance(structures, dict):
                        structures.pop("molfile", None)

                    # Transform synonyms into a grouped dictionary: {syn_type: [list of molecule_synonyms]}
                    raw_synonyms = primary_molecule.get("molecule_synonyms", [])
                    grouped_synonyms = defaultdict(list)
                    for syn in raw_synonyms:
                        syn_type = syn.get("syn_type")
                        syn_value = syn.get("molecule_synonym")
                        if syn_type and syn_value:
                            grouped_synonyms[syn_type].append(syn_value)

                    structural_payload = {
                        "identifier_searched": term_value,
                        "chembl_id": chembl_id,
                        "pref_name": primary_molecule.get("pref_name"),
                        "molecule_type": primary_molecule.get("molecule_type"),
                        "max_phase": primary_molecule.get("max_phase"),
                        "first_approval": primary_molecule.get("first_approval"),
                        "therapeutic_flag": primary_molecule.get("therapeutic_flag"),
                        "withdrawn_flag": primary_molecule.get("withdrawn_flag"),
                        "black_box_warning": primary_molecule.get("black_box_warning"),
                        "orphan": primary_molecule.get("orphan"),
                        "chirality": primary_molecule.get("chirality"),
                        "natural_product": primary_molecule.get("natural_product"),
                        "prodrug": primary_molecule.get("prodrug"),
                        "routes": {
                            "oral": primary_molecule.get("oral"),
                            "parenteral": primary_molecule.get("parenteral"),
                            "topical": primary_molecule.get("topical")
                        },
                        "atc_classifications": primary_molecule.get("atc_classifications", []),
                        "molecule_properties": primary_molecule.get("molecule_properties"),
                        "molecule_structures": structures,
                        "synonyms": dict(grouped_synonyms)
                    }

                    self.handle_task_success(
                        task, query_url, json.dumps(structural_payload),
                        f"Successfully retrieved structural profile for ChEMBL ID {chembl_id}.", task_log
                    )
                    self.save_progress_checkpoint(task_log)

                # CASE B: Discovery Task (Name, InChIKey, SMILES) -> Resolve ID, spawn task, finish.
                else:
                    mol_records = []

                    if 'INCHI' in term_type:
                        molecule_query = self.molecule_client.filter(
                            molecule_structures__standard_inchi_key=term_value
                        ).only('molecule_chembl_id')
                        mol_records = self._safe_fetch_records(molecule_query, label="InChIKey Discovery")

                    elif term_type == 'SMILES':
                        molecule_query = self.molecule_client.filter(
                            molecule_structures__canonical_smiles=term_value
                        ).only('molecule_chembl_id')
                        mol_records = self._safe_fetch_records(molecule_query, label="SMILES Discovery")

                    else:
                        pref_query = self.molecule_client.filter(pref_name__iexact=term_value).only('molecule_chembl_id')
                        mol_records = self._safe_fetch_records(pref_query, label="Preferred Name Discovery")

                        if not mol_records:
                            synonym_query = self.molecule_client.filter(
                                molecule_synonyms__molecule_synonym__iexact=term_value
                            ).only('molecule_chembl_id')
                            mol_records = self._safe_fetch_records(synonym_query, label="Synonym Discovery")

                    for r in mol_records:
                        if cid := r.get('molecule_chembl_id'):
                            discovered_chembl_ids.add(cid)

                    if not discovered_chembl_ids:
                        self.handle_task_not_found(
                            task, query_url,
                            f"No ChEMBL ID resolved for discovery term {term_value}.",
                            task_log
                        )
                        self.save_progress_checkpoint(task_log)
                        continue

                    spawned_count = 0
                    if current_source:
                        spawned_count = self._spawn_chembl_id_tasks(discovered_chembl_ids, current_source)

                    discovery_payload = {
                        "identifier_searched": term_value,
                        "term_type": term_type,
                        "resolved_chembl_ids": list(discovered_chembl_ids),
                        "new_tasks_spawned": spawned_count
                    }

                    self.handle_task_success(
                        task, query_url, json.dumps(discovery_payload),
                        f"Resolved {term_value} to ChEMBL IDs: {list(discovered_chembl_ids)}", task_log
                    )
                    self.save_progress_checkpoint(task_log)


            except Exception as e:
                self.logger.exception(f"Error executing ChEMBL provider task: {e}")
                self.handle_task_error(task, query_url, str(e), task_log)
                self.save_progress_checkpoint(task_log)

        task_log.save(update_fields=['success_count', 'not_found_count', 'error_count', 'processed_items'])

def execute_batch(tasks, task_log, *args, **kwargs):
    provider = ChEMBLProvider()
    provider.execute_batch(tasks, task_log, *args, **kwargs)

import os
import json
import urllib.parse
from django.conf import settings
from django.utils import timezone
from apps.registry.models import WebFinding, SearchTask, SearchTerm, UnclassifiedPathReview
from apps.registry.services.base import BaseProvider

PROVIDER_CONFIG = {
    "accepts_cids": True,
    "accepted_term_types": ["CID", "CAS", "NAME", "SMILES", "INCHI", "INCHIKEY"],
    "representations": ["search", "pug_view"]
}

# ==========================================
# PARSING & CURATION UTILITIES (Consolidated)
# ==========================================

def load_curation_lists():
    """Loads saved paths from disk and ignored paths from database/files."""
    saved_paths = set()
    ignored_paths = set()

    json_path = os.path.join(settings.BASE_DIR, 'apps', 'registry', 'data', 'pubchem_saved_paths.json')
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r') as f:
                for p in json.load(f):
                    saved_paths.add(p.strip())
        except Exception:
            pass

    ignored_json_path = os.path.join(settings.BASE_DIR, 'apps', 'registry', 'data', 'pubchem_ignored_paths.json')
    if os.path.exists(ignored_json_path):
        try:
            with open(ignored_json_path, 'r') as f:
                for p in json.load(f):
                    ignored_paths.add(p.strip())
        except Exception:
            pass

    return saved_paths, ignored_paths

def _sanitize_for_db(data):
    if isinstance(data, str):
        return data.encode('ascii', 'ignore').decode('ascii')
    elif isinstance(data, dict):
        return {k: _sanitize_for_db(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_sanitize_for_db(item) for item in data]
    return data

def stage_for_admin_review(path, section_data):
    clean_section_data = _sanitize_for_db(section_data)
    sample_snippet = clean_section_data.get('Information', [])[:2]

    obj, created = UnclassifiedPathReview.objects.get_or_create(
        path=path,
        defaults={'sample_data': sample_snippet}
    )
    if not created:
        obj.occurrence_count += 1
        obj.save()


def strip_section_metadata(data):
    """Recursively removes layout metadata, UI controls, URLs, and provenance bloat from section dictionaries."""
    if isinstance(data, dict):
        metadata_keys = [
            'Description', 'DisplayControls', 'LicenseNote', 
            'ReferenceNumber', 'Markup', 'URL', 'Reference', 'ExtendedReference'
        ]
        for metadata in metadata_keys:
            data.pop(metadata, None)

        for k, v in list(data.items()):
            strip_section_metadata(v)
    elif isinstance(data, list):
        for item in data:
            strip_section_metadata(item)
    return data


def flatten_and_clean_sections(data):
    """Recursively flattens PubChem leaf nodes, strips external table stubs, and prunes empty shells."""
    if isinstance(data, dict):
        # 1. Drop empty remote query placeholder stubs instantly
        if len(data) == 1 and 'ExternalTableName' in data:
            return None

        data.pop('ExtendedReference', None)
        data.pop('ReferenceNumber', None)
        data.pop('ExternalTableName', None)

        # 2. Process subsections recursively first
        if 'Section' in data and isinstance(data['Section'], list):
            cleaned_subs = []
            for sub in data['Section']:
                res = flatten_and_clean_sections(sub)
                if res is not None:
                    cleaned_subs.append(res)
            if cleaned_subs:
                data['Section'] = cleaned_subs
            else:
                data.pop('Section', None)

        # 3. Process 'Information' block values
        flat_vals = []
        if 'Information' in data and isinstance(data['Information'], list):
            for item in data['Information']:
                if isinstance(item, dict):
                    item.pop('ExtendedReference', None)
                    item.pop('ReferenceNumber', None)
                    val = item.get('Value', {})
                    
                    # Grab valid strings
                    for s in val.get('StringWithMarkup', []):
                        if isinstance(s, dict) and 'String' in s:
                            txt = s['String'].strip()
                            if txt:
                                flat_vals.append(txt)
                                
                    # Grab valid numbers
                    for n in val.get('Number', []):
                        if n is not None:
                            flat_vals.append(n)
            
            data.pop('Information', None)

        if flat_vals:
            if all(isinstance(x, str) for x in flat_vals):
                data['Values'] = list(dict.fromkeys(flat_vals))
            else:
                data['Values'] = flat_vals

        # 4. Drop section shell if it contains no Values, no Sections, and no Information left
        if 'TOCHeading' in data and not data.get('Values') and not data.get('Section'):
            return None

        return data

    elif isinstance(data, list):
        cleaned_list = []
        for item in data:
            res = flatten_and_clean_sections(item)
            if res is not None:
                cleaned_list.append(res)
        return cleaned_list

    return data


# ==========================================
# PUBCHEM PROVIDER IMPLEMENTATION
# ==========================================


class PubChemProvider(BaseProvider):
    provider_code = "pubchem"
    min_request_interval = 0.2

    def execute_batch(self, tasks, task_log, *args, **kwargs):
        for task in tasks:
            if self.cancellation_requested(task_log):
                break

            task_log.processed_items += 1
            task.run_count += 1
            task.last_run_at = timezone.now()

            target_rep = task.target_representation or 'search'

            if target_rep == 'pug_view':
                self._fetch_master_pug_view(task, task_log)
            else:
                self._execute_search_task(task, task_log)

            if task_log.processed_items % 5 == 0:
                task_log.save(update_fields=['processed_items', 'success_count', 'not_found_count', 'error_count'])

        task_log.save(update_fields=['success_count', 'not_found_count', 'error_count', 'processed_items'])

    def _get_pubchem_namespace(self, term_type, term_value):
        t_type = (term_type or "").upper()
        namespace_map = {
            "CID": "cid",
            "CAS": "xref/RN",
            "INCHIKEY": "inchikey",
            "SMILES": "smiles",
            "INCHI": "inchi",
            "NAME": "name"
        }

        for key, ns in namespace_map.items():
            if key in t_type:
                return ns

        if term_value.isdigit():
            return "cid"

        if "-" in term_value and len(term_value.split("-")) == 3:
            return "xref/RN"

        return "name"

    def _execute_search_task(self, task, task_log):
        term_value = task.search_term.term_value.strip() if task.search_term else ""
        if not term_value:
            self.handle_task_not_found(task, "", "Empty search term.", task_log)
            return

        term_type = task.search_term.term_type
        namespace = self._get_pubchem_namespace(term_type, term_value)
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/{namespace}/{urllib.parse.quote(term_value)}/cids/JSON"

        try:
            self.polite_throttle()
            response = self.session.get(url, timeout=10)

            if response.status_code in (404, 500):
                self.handle_task_not_found(task, url, f"Compound not found in PubChem via namespace '{namespace}'.", task_log)
                return

            response.raise_for_status()
            data = response.json()

            cids = data.get('IdentifierList', {}).get('CID', [])
            if not cids:
                self.handle_task_not_found(task, url, "PubChem search returned no matching CIDs.", task_log)
                return

            canonical_cid = str(cids[0])
            match_payload = {"searched_term": term_value, "namespace": namespace, "resolved_cid": canonical_cid, "all_cids": cids}
            
            self.handle_task_success(
                task, url, json.dumps(match_payload),
                f"Resolved term '{term_value}' to canonical PubChem CID {canonical_cid}", task_log
            )

            self._ensure_and_queue_pug_view_task(canonical_cid, task.web_source)

        except Exception as e:
            self.logger.exception(f"Error processing PubChem search task for '{term_value}': {e}")
            self.handle_task_error(task, url, str(e), task_log)

    def _fetch_master_pug_view(self, task, task_log):
        cid = task.search_term.term_value.strip() if task.search_term else ""
        pug_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON"

        try:
            self.polite_throttle()
            res = self.session.get(pug_url, timeout=20)

            if res.status_code == 404:
                self.handle_task_not_found(task, pug_url, f"PUG View record not found for CID {cid}.", task_log)
                return

            res.raise_for_status()
            data = res.json()

            record = data.get('Record', {})
            sections = record.get('Section', [])
            if not sections:
                self.logger.warning(f"[!] CID {cid}: Record has no sections (empty shell).")
                self.handle_task_not_found(task, pug_url, f"PUG View record for CID {cid} contains no sections (empty shell).", task_log)
                return

            saved_paths, ignored_paths = load_curation_lists()


            def prune_sections(sec_list, path="Record"):
                retained_sections = []
                for sec in sec_list:
                    heading = sec.get('TOCHeading', '')
                    current_path = f"{path} > {heading}" if heading else path

                    check_path = current_path
                    if check_path.startswith("Record > "):
                        check_path = check_path[9:]

                    if check_path in ignored_paths or current_path in ignored_paths:
                        continue

                    is_saved_exact = (check_path in saved_paths or current_path in saved_paths)
                    is_parent_container = any(sp.startswith(check_path + " >") for sp in saved_paths)

                    if not is_saved_exact and not is_parent_container:
                        stage_for_admin_review(current_path, sec)
                        continue

                    # Process subsections recursively
                    sub_sections = sec.get('Section', [])
                    if sub_sections:
                        sec['Section'] = prune_sections(sub_sections, current_path)

                    has_info = bool(sec.get('Information'))
                    has_sub = bool(sec.get('Section'))
                    if not has_info and not has_sub:
                        continue

                    retained_sections.append(sec)
                return retained_sections

            # 1. Prune against admin/saved curation pathways first
            record['Section'] = prune_sections(sections, "Record")

            if not record['Section']:
                self.logger.warning(f"[!] CID {cid}: Record became empty after curation pruning.")
                self.handle_task_not_found(task, pug_url, f"PUG View record for CID {cid} became empty after curation pruning.", task_log)
                return

            # 2. Strip metadata and flatten/clean leaf nodes in one consolidated recursive pass
            strip_section_metadata(record)
            record['Section'] = flatten_and_clean_sections(record['Section'])

            clean_payload = _sanitize_for_db(data)

            self.handle_task_success(
                task, pug_url, json.dumps(clean_payload),
                f"Master PUG View record pruned and parsed successfully for CID {cid}.", task_log
            )

        except Exception as e:
            self.logger.exception(f"Error fetching PUG View for Master CID {cid}: {e}")
            self.handle_task_error(task, pug_url, str(e), task_log)

    def _ensure_and_queue_pug_view_task(self, cid, web_source):
        search_term, _ = SearchTerm.objects.get_or_create(
            term_value=cid,
            defaults={'term_type': 'CID', 'frequency_count': 1}
        )

        pug_task, created = SearchTask.objects.get_or_create(
            search_term=search_term,
            web_source=web_source,
            target_representation='pug_view',
            defaults={
                'status': SearchTask.TaskStatus.PENDING,
                'is_completed': False
            }
        )
        return pug_task

def execute_batch(tasks, task_log, *args, **kwargs):
    provider = PubChemProvider()
    provider.execute_batch(tasks, task_log, *args, **kwargs)

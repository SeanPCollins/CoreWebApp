from collections import defaultdict
import json
import ctxpy as ctx
import time
import math
from django.conf import settings
from django.utils import timezone
from apps.registry.models import WebFinding, SearchTask, SearchTerm
from apps.registry.services.base import BaseProvider

PROVIDER_CONFIG = {
    "accepts_cids": False,
    "accepted_term_types": ["CAS", "NAME", "DTXSID", "SMILES", "INCHI", "INCHIKEY"],
    "representations": ["search", "detail"]
}

# ==========================================
# 1. CORE CLEANING & NORMALIZATION UTILITIES
# ==========================================

def clean_value(v):
    """Strictly filter out None, empty strings, whitespace-padded sentinels, and NA values."""
    if v is None:
        return None
    if isinstance(v, str):
        v_stripped = v.strip()
        if v_stripped in ["NA", "N/A", "-999", "-999.0", ""]:
            return None
        return v_stripped
    if v in [-999, -999.0]:
        return None
    return v


def clean_records(records, drop_keys=None, keep_identifiers=False):
    """Unified generic cleaner. Only drops nulls, NaNs, and identifiers (unless keep_identifiers=True)."""
    if not isinstance(records, list) or not records:
        return []
    default_drops = set() if keep_identifiers else {"dtxsid", "casrn"}
    if drop_keys:
        default_drops.update(drop_keys)

    cleaned_list = []
    for rec in records:
        cleaned = {}
        for k, v in rec.items():
            if k in default_drops:
                continue
            if v is None:
                continue
            if isinstance(v, float) and math.isnan(v):
                continue

            cleaned_val = clean_value(v)
            if cleaned_val is not None:
                cleaned[k] = cleaned_val

        if cleaned:
            cleaned_list.append(cleaned)
    return cleaned_list


# ==========================================
# 2. DOMAIN NESTING ENGINES
# ==========================================

def clean_and_nest_adme_ivive(records):
    if not isinstance(records, list) or not records:
        return []

    grouped = defaultdict(lambda: defaultdict(list))
    data_source_species = "Human"
    found_ds_species = False

    for rec in records:
        cleaned = {k: clean_value(v) for k, v in rec.items() if clean_value(v) is not None}
        cleaned.pop("dtxsid", None)
        cleaned.pop("id", None)

        if not found_ds_species and cleaned.get("dataSourceSpecies"):
            data_source_species = cleaned.get("dataSourceSpecies")
            found_ds_species = True

        cleaned.pop("dataSourceSpecies", None)
        species = cleaned.pop("species", "General")
        description = cleaned.pop("description", "Measurement")

        grouped[species][description].append(cleaned)

    species_list = [
        {
            "species": species,
            "parameters": [
                {"parameter": desc, "measurements": items}
                for desc, items in desc_dict.items()
            ]
        }
        for species, desc_dict in grouped.items()
    ]

    return {
        "dataSourceSpecies": data_source_species,
        "toxicokinetics": species_list
    }


def clean_and_nest_mmdb(records):
    if not isinstance(records, list) or not records:
        return []

    PURGE_KEYS = {
        "dtxsid", "DTXSID", "casrn", "CASRN",
        "chemicalName", "preferredName", "exportDate", "qualityFlag",
        "method", "reference", "version"
    }

    nested_tree = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))))
    year_meta_tracker = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(set))))

    for rec in records:
        cleaned = {k: clean_value(v) for k, v in rec.items() if clean_value(v) is not None}

        source = cleaned.pop("fullSourceName", "Unknown Source")
        country = cleaned.pop("country", "Unknown Country")
        state = cleaned.pop("stateOrProvince", "Unknown Location")
        year = cleaned.pop("year", "Unknown Year")
        h_medium = cleaned.pop("harmonizedMedium", "unknown medium")
        media = cleaned.pop("media", "unknown media")

        method = cleaned.get("method")
        reference = cleaned.get("reference")
        version = cleaned.get("version")

        if method: year_meta_tracker[source][country][state][year].add(("method", method))
        if reference: year_meta_tracker[source][country][state][year].add(("reference", reference))
        if version: year_meta_tracker[source][country][state][year].add(("version", version))

        for key in PURGE_KEYS:
            cleaned.pop(key, None)

        nested_tree[source][country][state][year][h_medium][media].append(cleaned)

    payload_tree = []
    for source, countries in nested_tree.items():
        country_list = []
        for country, states in countries.items():
            state_list = []
            for state, years in states.items():
                year_list = []
                for year, h_mediums in years.items():
                    meta_entries = year_meta_tracker[source][country][state][year]
                    year_node = {
                        "year": year if year != "Unknown Year" else None,
                        "method": next((val for k, val in meta_entries if k == "method"), None),
                        "reference": next((val for k, val in meta_entries if k == "reference"), None),
                        "version": next((val for k, val in meta_entries if k == "version"), None),
                    }

                    medium_list = []
                    for h_medium, media_dict in h_mediums.items():
                        media_list = []
                        for media, recs in media_dict.items():
                            media_list.append({
                                "media": media if media != "unknown media" else None,
                                "records": recs
                            })

                        medium_list.append({
                            "harmonizedMedium": h_medium if h_medium != "unknown medium" else None,
                            "media_groups": media_list
                        })

                    year_node["harmonized_mediums"] = medium_list
                    year_list.append({k: v for k, v in year_node.items() if v is not None or k == "year"})

                state_list.append({
                    "stateOrProvince": state if state != "Unknown Location" else None,
                    "years": year_list
                })

            country_list.append({
                "country": country if country != "Unknown Country" else None,
                "states": state_list
            })

        payload_tree.append({
            "fullSourceName": source,
            "countries": country_list
        })

    return payload_tree


def clean_and_nest_httk(records):
    if not isinstance(records, list) or not records:
        return []

    PURGE_KEYS = {"dtxsid", "DTXSID", "casrn", "CASRN"}
    grouped = defaultdict(lambda: defaultdict(list))

    for rec in records:
        cleaned = {k: clean_value(v) for k, v in rec.items() if clean_value(v) is not None}
        species = cleaned.pop("species", "General Species")
        parameter = cleaned.pop("parameter", "General Parameter")

        for key in PURGE_KEYS:
            cleaned.pop(key, None)

        grouped[species][parameter].append(cleaned)

    species_list = []
    for species, parameters in grouped.items():
        param_list = []
        for parameter, recs in parameters.items():
            param_list.append({
                "parameter": parameter if parameter != "General Parameter" else None,
                "records": recs
            })

        species_list.append({
            "species": species if species != "General Species" else None,
            "parameters": param_list
        })

    return species_list


def clean_and_nest_toxvaldb(domain_key, records):
    PURGE_KEYS = {"dtxsid", "preferredName", "casrn", "name", "chemicalName", "latinName"}
    SOURCE_META_KEYS = {"sourceUrl", "longRef", "title", "author", "year", "originalYear"}

    if not isinstance(records, list) or not records:
        return []

    if domain_key != "all":
        grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

        for rec in records:
            cleaned = {k: clean_value(v) for k, v in rec.items() if clean_value(v) is not None}
            for key in PURGE_KEYS:
                cleaned.pop(key, None)

            source = cleaned.pop("source", "Unknown Source")
            species = cleaned.pop("species", "General Species")
            assay_cat = cleaned.pop("assayCategory", "General Assay")

            grouped[source][species][assay_cat].append(cleaned)

        source_list = []
        for source, species_dict in grouped.items():
            species_list = []
            for species, assay_dict in species_dict.items():
                assay_list = [
                    {
                        "assayCategory": assay_cat if assay_cat != "General Assay" else None,
                        "findings": findings
                    }
                    for assay_cat, findings in assay_dict.items()
                ]
                cleaned_assay_list = [{k: v for k, v in assay.items() if v is not None} for assay in assay_list]

                species_node = {
                    "species": species if species != "General Species" else None,
                    "assay_categories": cleaned_assay_list
                }
                species_list.append({k: v for k, v in species_node.items() if v is not None})

            source_list.append({
                "source": source,
                "species_groups": species_list
            })

        return source_list

    sources_dict = defaultdict(lambda: {
        "sourceUrl": None, "longRef": None, "title": None, "author": None,
        "year": None, "originalYear": None, "super_categories": defaultdict(list)
    })

    for rec in records:
        cleaned = {k: clean_value(v) for k, v in rec.items() if clean_value(v) is not None}
        for key in PURGE_KEYS:
            cleaned.pop(key, None)

        source = cleaned.pop("source", "Unknown Source")

        for meta_key in SOURCE_META_KEYS:
            if meta_key in cleaned:
                val = cleaned.pop(meta_key, None)
                if val is not None and sources_dict[source][meta_key] is None:
                    sources_dict[source][meta_key] = val

        super_cat = cleaned.pop("toxvalTypeSuperCategory", "Uncategorized")
        sources_dict[source]["super_categories"][super_cat].append(cleaned)

    payload_tree = []
    for source, data in sources_dict.items():
        super_cats_list = [
            {"toxvalTypeSuperCategory": super_cat, "records": recs}
            for super_cat, recs in data["super_categories"].items()
        ]

        source_node = {
            "source": source, "sourceUrl": data["sourceUrl"], "longRef": data["longRef"],
            "title": data["title"], "author": data["author"], "year": data["year"],
            "originalYear": data["originalYear"], "categories": super_cats_list
        }
        payload_tree.append({k: v for k, v in source_node.items() if v is not None or k == "categories"})

    return payload_tree


# ==========================================
# 3. LOOKUP REGISTRY COMPRESSION UTILITIES
# ==========================================

def gather_string_frequencies(node, freq_map, min_char_length=100):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and len(v.strip()) >= min_char_length:
                freq_map[v] += 1
            else:
                gather_string_frequencies(v, freq_map, min_char_length)
    elif isinstance(node, list):
        for item in node:
            gather_string_frequencies(item, freq_map, min_char_length)


def normalize_records_in_place(records, lookup_registry, multi_use_strings, min_char_length=100):
    normalized_records = []
    for rec in records:
        if not isinstance(rec, dict):
            normalized_records.append(rec)
            continue

        cleaned_rec = {}
        for k, v in rec.items():
            if v is not None and isinstance(v, str) and len(v.strip()) >= min_char_length and multi_use_strings.get(v, 0) >= 2:
                val_key = f"{k}_{abs(hash(v))}"
                if val_key not in lookup_registry:
                    lookup_registry[val_key] = {"field": k, "value": v}
                cleaned_rec[f"{k}_ref"] = val_key
            else:
                cleaned_rec[k] = v
        if cleaned_rec:
            normalized_records.append(cleaned_rec)
    return normalized_records


def recursive_normalize_tree(node, lookup_registry, multi_use_strings, min_char_length=100):
    if isinstance(node, dict):
        new_node = {}
        for k, v in node.items():
            if k in {"records", "measurements", "findings"} and isinstance(v, list):
                new_node[k] = normalize_records_in_place(v, lookup_registry, multi_use_strings, min_char_length)
            else:
                new_node[k] = recursive_normalize_tree(v, lookup_registry, multi_use_strings, min_char_length)
        return new_node
    elif isinstance(node, list):
        return [recursive_normalize_tree(item, lookup_registry, multi_use_strings, min_char_length) for item in node]
    else:
        return node

def process_json_payload(raw_payload, min_char_length=100):
    freq_map = defaultdict(int)
    gather_string_frequencies(raw_payload, freq_map, min_char_length)
    multi_use_strings = {val: count for val, count in freq_map.items() if count >= 2}

    if not multi_use_strings:
        return raw_payload

    lookup_registry = {}
    normalized_data = recursive_normalize_tree(raw_payload, lookup_registry, multi_use_strings, min_char_length)

    return {"lookup_registry": lookup_registry, "payload": normalized_data}


# ==========================================
# 4. COMPTOX PROVIDER & BATCH EXECUTION
# ==========================================

class ComptoxProvider(BaseProvider):
    provider_code = "comptox"
    min_request_interval = 0.5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        api_key = getattr(settings, 'COMPTOX_API_KEY', None) or getattr(settings, 'CTX_X_API_KEY', None)
        if not api_key:
            raise ValueError("CompTox API key is missing. Ensure COMPTOX_API_KEY is defined in your Django settings.")

        self.chem_client = ctx.Chemical(x_api_key=api_key)
        self.hazard_client = ctx.Hazard(x_api_key=api_key)
        self.exposure_client = ctx.Exposure(x_api_key=api_key)


    def _safe_fetch_and_clean(self, fetch_func, post_processor=None, drop_keys=None, keep_identifiers=False, label="", **kwargs):
        t_start = time.time()
        raw_result = None

        for attempt in range(1, 4):
            try:
                self.polite_throttle()
                raw_result = fetch_func(**kwargs)
                break
            except Exception as net_err:
                if "RemoteDisconnected" in str(net_err) and attempt < 3:
                    time.sleep(2 * attempt)
                    continue
                raise net_err

        if hasattr(raw_result, 'to_dict'):
            recs = raw_result.to_dict(orient='records') if not raw_result.empty else []
        elif isinstance(raw_result, list):
            recs = raw_result
        elif isinstance(raw_result, dict):
            recs = [raw_result]
        else:
            recs = []

        if post_processor:
            if label and "ToxValDB" in label:
                domain_key = kwargs.get('by', 'all')
                cleaned_data = post_processor(domain_key, recs)
            else:
                cleaned_data = post_processor(recs)
        else:
            cleaned_data = clean_records(recs, drop_keys=drop_keys, keep_identifiers=keep_identifiers)
        return cleaned_data

    def execute_batch(self, tasks, task_log, *args, **kwargs):
        """Dispatcher: Routes tasks based on their target_representation ('search' vs 'detail')."""
        for task in tasks:
            if self.cancellation_requested(task_log):
                break

            task.run_count += 1
            task.last_run_at = timezone.now()
            task.save(update_fields=['run_count', 'last_run_at'])
            task_log.processed_items += 1

            target_rep = task.target_representation or 'search'

            if target_rep == 'detail':
                self._execute_detail_task(task, task_log)
            else:
                self._execute_search_task(task, task_log)

            if task_log.processed_items % 5 == 0:
                task_log.save(update_fields=['processed_items', 'success_count', 'not_found_count', 'error_count'])

        task_log.save(update_fields=['success_count', 'not_found_count', 'error_count', 'processed_items'])

    def _execute_search_task(self, task, task_log):
        """Phase 1: Discovery search phase. Resolves raw terms (Names, CAS, etc.) to a DTXSID."""
        term_value = task.search_term.term_value.strip() if task.search_term else ""
        if not term_value:
            self.handle_task_not_found(task, "", "Empty search term.", task_log)
            return

        query_url = f"comptox://chem/search?term={term_value}"

        try:
            # If the incoming term is already a DTXSID, skip searching and queue detail directly
            if term_value.upper().startswith("DTXSID"):
                canonical_dsid = term_value.upper()
            else:
                search_results = self._safe_fetch_and_clean(
                    fetch_func=self.chem_client.search,
                    by='batch',
                    query=[term_value],
                    label="Term-to-DTXSID Resolution",
                    keep_identifiers=True
                )

                if not search_results:
                    self.handle_task_not_found(task, query_url, f"No matching substance found in CompTox for '{term_value}'.", task_log)
                    return

                match = search_results[0]

                # Fallback extraction
                if hasattr(match, 'get'):
                    canonical_dsid = (
                        match.get('dtxsid') or match.get('DTXSID') or 
                        match.get('dsstoxSubstanceId') or match.get('substanceId') or match.get('id')
                    )
                else:
                    canonical_dsid = (
                        getattr(match, 'dtxsid', None) or getattr(match, 'DTXSID', None) or 
                        getattr(match, 'dsstoxSubstanceId', None) or getattr(match, 'substanceId', None) or getattr(match, 'id', None)
                    )

                if not canonical_dsid:
                    self.handle_task_not_found(task, query_url, f"CompTox match found for '{term_value}', but missing DTXSID.", task_log)
                    return

            # Save Search Finding
            match_payload = {"searched_term": term_value, "resolved_dtxsid": canonical_dsid}
            self.handle_task_success(
                task, query_url, json.dumps(match_payload),
                f"Resolved term '{term_value}' to DTXSID {canonical_dsid}", task_log
            )

            # AFTER successful search, queue the dedicated detail task
            self._ensure_and_queue_detail_task(canonical_dsid, task.web_source)

        except Exception as e:
            self.logger.exception(f"Error processing CompTox search task for '{term_value}': {e}")
            self.handle_task_error(task, query_url, str(e), task_log)

    def _execute_detail_task(self, task, task_log):
        """Phase 2: Deep comprehensive record ingestion using the verified DTXSID."""
        dsid = task.search_term.term_value.strip().upper() if task.search_term else ""
        if not dsid.startswith("DTXSID"):
            self.handle_task_not_found(task, "", f"Invalid DTXSID for detail task: '{dsid}'.", task_log)
            return

        query_url = f"comptox://dtxsid/{dsid}/comprehensive"

        try:
            # 1. Master Chemical Details
            chem_records = self._safe_fetch_and_clean(
                fetch_func=self.chem_client.details,
                by='dtxsid',
                query=dsid,
                subset='all',
                keep_identifiers=True,
                label="Master Details"
            )

            chem_record = chem_records[0] if chem_records else {}
            if not chem_record:
                self.handle_task_not_found(task, query_url, f"No master chemical record found for DTXSID {dsid}.", task_log)
                return

            cleaned_chem = clean_records([chem_record], keep_identifiers=True)[0]
            hazard_payload = {}
            exposure_payload = {}

            # 2. ToxValDB Sub-domains
            for sub in ['all', 'skin-eye', 'cancer', 'genetox', 'genetox-summary']:
                try:
                    hazard_payload[f"toxvaldb_{sub}"] = self._safe_fetch_and_clean(
                        fetch_func=self.hazard_client.search_toxvaldb,
                        post_processor=clean_and_nest_toxvaldb,
                        by=sub,
                        dtxsid=dsid,
                        label=f"ToxValDB '{sub}'"
                    )
                except Exception as e:
                    hazard_payload[f"toxvaldb_{sub}"] = {"error": str(e)}

            # 3. ToxRefDB Domains
            for domain in ['effects', 'summary', 'data', 'observations']:
                try:
                    hazard_payload[f"toxrefdb_{domain}"] = self._safe_fetch_and_clean(
                        fetch_func=self.hazard_client.search_toxrefdb,
                        by='dtxsid',
                        domain=domain,
                        query=dsid,
                        label=f"ToxRefDB '{domain}'"
                    )
                except Exception as e:
                    hazard_payload[f"toxrefdb_{domain}"] = {"error": str(e)}

            # 4. IRIS, PPRTV, HAWC
            for source_name, fetch_func in [
                ('iris', self.hazard_client.search_iris),
                ('pprtv', self.hazard_client.search_pprtv),
                ('hawc', self.hazard_client.search_hawc)
            ]:
                try:
                    hazard_payload[source_name] = self._safe_fetch_and_clean(
                        fetch_func=fetch_func,
                        dtxsid=dsid,
                        label=source_name.upper()
                    )
                except Exception as e:
                    hazard_payload[source_name] = {"error": str(e)}

            # 5. ADME-IVIVE
            try:
                recs = self._safe_fetch_and_clean(
                    fetch_func=self.hazard_client.search_adme_ivive,
                    dtxsid=dsid,
                    label="ADME-IVIVE"
                )
                hazard_payload["adme_ivive"] = clean_and_nest_adme_ivive(recs)
            except Exception as e:
                hazard_payload["adme_ivive"] = {"error": str(e)}

            # 6. Exposure (QSUR, Pathways, SEEM, HTTK, MMDB)
            try:
                exposure_payload["qsurs"] = self._safe_fetch_and_clean(self.exposure_client.search_qsurs, dtxsid=dsid, label="QSUR")
            except Exception:
                exposure_payload["qsurs"] = []

            try:
                exposure_payload["exposures_pathways"] = self._safe_fetch_and_clean(self.exposure_client.search_exposures, by='pathways', dtxsid=dsid, label="Pathways")
            except Exception:
                exposure_payload["exposures_pathways"] = []

            try:
                exposure_payload["exposures_seem"] = self._safe_fetch_and_clean(self.exposure_client.search_exposures, by='seem', dtxsid=dsid, label="SEEM")
            except Exception:
                exposure_payload["exposures_seem"] = []

            try:
                httk_raw = self._safe_fetch_and_clean(self.exposure_client.search_httk, dtxsid=dsid, label="HTTK")
                exposure_payload["httk"] = clean_and_nest_httk(httk_raw)
            except Exception:
                exposure_payload["httk"] = []

            try:
                exposure_payload["mmdb"] = self._safe_fetch_and_clean(
                    fetch_func=self.exposure_client.search_mmdb,
                    post_processor=clean_and_nest_mmdb,
                    by='dtxsid',
                    query=dsid,
                    label="MMDB"
                )
            except Exception as e:
                exposure_payload["mmdb"] = {"error": str(e)}

            raw_comprehensive_payload = {
                "dtxsid": dsid,
                "chemical_details": cleaned_chem,
                "hazard_data": hazard_payload,
                "exposure_data": exposure_payload
            }

            compressed_payload = process_json_payload(raw_comprehensive_payload)

            self.handle_task_success(
                task, query_url, json.dumps(compressed_payload),
                f"Comprehensive normalized chemical, hazard, and exposure bundle captured for {dsid}.", task_log
            )

        except Exception as e:
            self.logger.exception(f"Error in CompTox detail task for DTXSID {dsid}: {e}")
            self.handle_task_error(task, query_url, str(e), task_log)

    def _ensure_and_queue_detail_task(self, dsid, web_source):
        """Creates a dedicated detail SearchTask for the verified DTXSID if it doesn't already exist."""
        search_term, _ = SearchTerm.objects.get_or_create(
            term_value=dsid,
            defaults={'term_type': 'DTXSID', 'frequency_count': 1}
        )

        detail_task, created = SearchTask.objects.get_or_create(
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
    provider = ComptoxProvider()
    provider.execute_batch(tasks, task_log, *args, **kwargs)

import json
import logging
import math
from django.db.models import Q
from apps.registry.models import WebFinding, Structure

logger = logging.getLogger(__name__)

def _safe_load_payload(raw):
    """Safely decodes raw payloads whether stored as dict, JSON string, or plain text."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw_clean = raw.strip()
        if raw_clean.startswith('{') or raw_clean.startswith('['):
            try:
                return json.loads(raw_clean)
            except json.JSONDecodeError:
                pass
        # Fallback for plain text payloads stored as strings
        return {"raw_text": raw_clean}
    return {}


def _number(value):
    if value in (None, ''):
        return None
    try:
        number = float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ==============================================================================
# PROVIDER PARSERS
# ==============================================================================

def parse_comptox_provider(findings_qs):
    """Parses CompTox findings safely."""
    extracted = []
    stats = {"success": 0, "ignored_empty": 0, "ignored_stubs": 0, "errors": 0}

    for finding in findings_qs:
        task = finding.search_task
        if not task:
            stats["errors"] += 1
            continue

        if task.target_representation.lower() != 'detail':
            stats["ignored_stubs"] += 1
            continue

        data = _safe_load_payload(finding.raw_payload)
        if not isinstance(data, dict):
            stats["errors"] += 1
            continue

        payload_obj = data.get('payload', data)
        if not isinstance(payload_obj, dict):
            stats["ignored_empty"] += 1
            continue

        chem_details = payload_obj.get('chemical_details', {})
        if isinstance(chem_details, list) and chem_details:
            chem_details = chem_details[0]

        if not isinstance(chem_details, dict) or not any(
            chem_details.get(field)
            for field in ('inchikey', 'inchiString', 'smiles', 'qsarReadySmiles')
        ):
            stats["ignored_empty"] += 1
            continue

        stats["success"] += 1
        extracted.append({
            "provider": "CompTox",
            "finding_id": str(finding.pk),
            "dtxsid": payload_obj.get('dtxsid') or chem_details.get('dtxsid'),
            "inchikey": chem_details.get('inchikey'),
            "inchi": chem_details.get('inchiString'),
            "canonical_smiles": chem_details.get('smiles') or chem_details.get('qsarReadySmiles'),
            "molecular_formula": chem_details.get('molFormula'),
            "molecular_weight": chem_details.get('averageMass'),
            "exact_mass": chem_details.get('monoisotopicMass'),
            "preferred_name": chem_details.get('preferredName'),
        })

    return extracted, stats


def parse_chemspider_provider(findings_qs):
    """Parses ChemSpider findings safely, handling string or dict payloads."""
    extracted = []
    stats = {"success": 0, "ignored_empty": 0, "ignored_stubs": 0, "errors": 0}

    for finding in findings_qs:
        data = _safe_load_payload(finding.raw_payload)
        if not isinstance(data, dict):
            stats["errors"] += 1
            continue

        if "resolved_chemspider_ids" in data or "new_tasks_spawned" in data:
            stats["ignored_stubs"] += 1
            continue

        inchikey = data.get('inchikey_openbabel') or data.get('inchikey')
        if not any((inchikey, data.get('inchi_openbabel'), data.get('inchi'), data.get('smiles'))):
            stats["ignored_empty"] += 1
            continue

        stats["success"] += 1
        extracted.append({
            "provider": "ChemSpider",
            "finding_id": str(finding.pk),
            "dtxsid": None,
            "inchikey": inchikey,
            "inchi": data.get('inchi_openbabel') or data.get('inchi'),
            "canonical_smiles": data.get('smiles'),
            "molecular_formula": data.get('formula'),
            "molecular_weight": data.get('molecular_weight') or data.get('average_mass'),
            "exact_mass": data.get('monoisotopic_mass'),
            "preferred_name": data.get('common_name'),
        })

    return extracted, stats


def parse_cactus_provider(findings_qs):
    """Parses NCI CACTUS findings safely."""
    extracted = []
    stats = {"success": 0, "ignored_empty": 0, "ignored_stubs": 0, "errors": 0}

    for finding in findings_qs:
        data = _safe_load_payload(finding.raw_payload)
        raw_text = str(data.get('raw_text', '')).strip()

        for prefix in ["inchikey=", "stdinchikey=", "smiles=", "iupac_name=", "formula="]:
            if raw_text.lower().startswith(prefix):
                raw_text = raw_text[len(prefix):].strip()

        if not raw_text or "error" in raw_text.lower() or "not found" in raw_text.lower():
            stats["ignored_empty"] += 1
            continue

        task = finding.search_task
        url = (finding.source_url or '').lower()
        rep = getattr(task, 'target_representation', '').lower() if task else ''

        record = {
            "provider": "CACTUS",
            "finding_id": str(finding.pk),
            "dtxsid": None,
            "inchikey": None,
            "inchi": None,
            "canonical_smiles": None,
            "molecular_formula": None,
            "molecular_weight": None,
            "exact_mass": None,
            "preferred_name": None,
        }

        if 'inchikey' in url or 'inchikey' in rep or 'stdinchikey' in url:
            record["inchikey"] = raw_text
        elif 'smiles' in url or 'smiles' in rep:
            record["canonical_smiles"] = raw_text
        elif 'iupac_name' in url or 'iupac_name' in rep or 'name' in url:
            record["preferred_name"] = raw_text
        elif 'formula' in url or 'formula' in rep:
            record["molecular_formula"] = raw_text
        else:
            record["preferred_name"] = raw_text

        stats["success"] += 1
        extracted.append(record)

    return extracted, stats


def parse_chembl_provider(findings_qs):
    """Parses ChEMBL findings, handling detailed chemical records and raw text search stubs."""
    extracted = []
    stats = {"success": 0, "ignored_empty": 0, "ignored_stubs": 0, "errors": 0}

    for finding in findings_qs:
        data = _safe_load_payload(finding.raw_payload)
        if not isinstance(data, dict):
            stats["errors"] += 1
            continue

        if (
            "raw_text" in data
            and not data.get("chembl_id")
            and not data.get("standard_inchi_key")
            and not data.get("inchikey")
            and not data.get("molecule_structures")
        ):
            stats["ignored_stubs"] += 1
            continue

        inchikey = data.get('standard_inchi_key') or data.get('inchikey')
        inchi = data.get('standard_inchi') or data.get('inchi')
        smiles = data.get('canonical_smiles') or data.get('smiles')
        pref_name = data.get('pref_name') or data.get('preferred_name')

        mol_structs = data.get('molecule_structures')
        mol_properties = data.get('molecule_properties')
        if not isinstance(mol_properties, dict):
            mol_properties = {}
        if isinstance(mol_structs, dict):
            inchikey = inchikey or mol_structs.get('standard_inchi_key') or mol_structs.get('inchikey')
            inchi = inchi or mol_structs.get('standard_inchi') or mol_structs.get('inchi')
            smiles = smiles or mol_structs.get('canonical_smiles') or mol_structs.get('smiles')
        elif isinstance(mol_structs, list) and mol_structs:
            for ms in mol_structs:
                if isinstance(ms, dict):
                    inchikey = inchikey or ms.get('standard_inchi_key') or ms.get('inchikey')
                    inchi = inchi or ms.get('standard_inchi') or ms.get('inchi')
                    smiles = smiles or ms.get('canonical_smiles') or ms.get('smiles')

        if not any((inchikey, inchi, smiles)):
            stats["ignored_empty"] += 1
            continue

        stats["success"] += 1
        extracted.append({
            "provider": "ChEMBL",
            "finding_id": str(finding.pk),
            "dtxsid": None,
            "inchikey": inchikey,
            "inchi": inchi,
            "canonical_smiles": smiles,
            "molecular_formula": data.get('full_molformula') or data.get('formula'),
            "molecular_weight": (
                data.get('full_mwt')
                or data.get('molecular_weight')
                or mol_properties.get('mw_freebase')
            ),
            "exact_mass": (
                mol_properties.get('mw_monoisotopic')
                or data.get('exact_mass')
            ),
            "preferred_name": pref_name,
        })

    return extracted, stats


def parse_pubchem_provider(findings_qs):
    """Parses PubChem pug_view JSON payloads and raw text search stubs."""
    extracted = []
    stats = {"success": 0, "ignored_empty": 0, "ignored_stubs": 0, "errors": 0}

    for finding in findings_qs:
        data = _safe_load_payload(finding.raw_payload)
        if not isinstance(data, dict):
            stats["errors"] += 1
            continue

        if "raw_text" in data and "Record" not in data:
            stats["ignored_stubs"] += 1
            continue

        record_obj = data.get("Record")
        if not isinstance(record_obj, dict):
            stats["ignored_empty"] += 1
            continue

        pref_name = record_obj.get("RecordTitle")
        inchikey, inchi, smiles, formula, mw, exact_mass, dtxsid = (
            None, None, None, None, None, None, None
        )

        def extract_from_sections(sections):
            nonlocal inchikey, inchi, smiles, formula, mw, exact_mass, dtxsid
            if not isinstance(sections, list):
                return
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                heading = sec.get("TOCHeading", "")
                for info in sec.get("Information", []):
                    val = info.get("Value", {})
                    for item in val.get("StringWithMarkup", []):
                        if isinstance(item, dict) and "String" in item:
                            s_val = item["String"]
                            if heading == "InChIKey" and not inchikey:
                                inchikey = s_val
                            elif heading == "InChI" and not inchi:
                                inchi = s_val
                            elif heading == "SMILES" and not smiles:
                                smiles = s_val
                            elif heading == "Molecular Formula" and not formula:
                                formula = s_val
                            elif heading in ("Exact Mass", "Monoisotopic Mass") and not exact_mass:
                                exact_mass = s_val
                            elif heading == "DSSTox Substance ID" and not dtxsid:
                                dtxsid = s_val

                    num_list = val.get("Number", [])
                    if heading == "Molecular Weight" and not mw and num_list:
                        mw = num_list[0]

                sub_secs = sec.get("Section", [])
                if sub_secs:
                    extract_from_sections(sub_secs)

        extract_from_sections(record_obj.get("Section", []))

        if not any((inchikey, inchi, smiles)):
            stats["ignored_empty"] += 1
            continue

        stats["success"] += 1
        extracted.append({
            "provider": "PubChem",
            "finding_id": str(finding.pk),
            "dtxsid": dtxsid,
            "inchikey": inchikey,
            "inchi": inchi,
            "canonical_smiles": smiles,
            "molecular_formula": formula,
            "molecular_weight": _number(mw),
            "exact_mass": _number(exact_mass),
            "preferred_name": pref_name,
        })

    return extracted, stats


def parse_cas_provider(findings_qs):
    """Parses CAS Common Chemistry findings."""
    extracted = []
    stats = {"success": 0, "ignored_empty": 0, "ignored_stubs": 0, "errors": 0}

    for finding in findings_qs:
        data = _safe_load_payload(finding.raw_payload)
        if not isinstance(data, dict):
            stats["errors"] += 1
            continue

        if (
            "inchikeyKey" not in data
            and "inchikey" not in data
            and "inchiKey" not in data
            and "inchi" not in data
            and "smile" not in data
            and "canonicalSmile" not in data
        ):
            stats["ignored_stubs"] += 1
            continue

        inchikey = data.get('inchikeyKey') or data.get('inchikey') or data.get('inchiKey')
        mw_str = data.get('molecularMass')
        mw = _number(mw_str)

        stats["success"] += 1
        extracted.append({
            "provider": "CAS Common Chemistry",
            "finding_id": str(finding.pk),
            "dtxsid": None,
            "inchikey": inchikey,
            "inchi": data.get('inchi'),
            "canonical_smiles": data.get('canonicalSmile') or data.get('smile'),
            "molecular_formula": data.get('molecularFormula'),
            "molecular_weight": mw,
            "exact_mass": None,
            "preferred_name": data.get('name'),
        })

    return extracted, stats


# ==============================================================================
# PROVIDER CONFIGS
# ==============================================================================
PROVIDER_CONFIGS = {
    "comptox": {
        "keywords": ["comptox"],
        "display_name": "CompTox Chemicals Dashboard",
        "parser": parse_comptox_provider
    },
    "chemspider": {
        "keywords": ["chemspider"],
        "display_name": "ChemSpider",
        "parser": parse_chemspider_provider
    },
    "cactus": {
        "keywords": ["cactus", "nci.nih.gov"],
        "display_name": "NCI CACTUS",
        "parser": parse_cactus_provider
    },
    "chembl": {
        "keywords": ["chembl"],
        "display_name": "ChEMBL Web Services",
        "parser": parse_chembl_provider
    },
    "pubchem": {
        "keywords": ["pubchem"],
        "display_name": "PubChem API",
        "parser": parse_pubchem_provider
    },
    "cas": {
        "keywords": ["cas common chemistry", "common chemistry"],
        "display_name": "CAS Common Chemistry",
        "parser": parse_cas_provider
    },
}


# ==============================================================================
# MASTER PIPELINE RUNNER (Web-Ready)
# ==============================================================================

def run_extraction_pipeline(target_provider=None):
    """
    Runs the extraction pipeline and returns both extracted records and global stats.
    Returns:
        tuple: (all_extracted_records (list), global_stats (dict))
    """
    all_findings = WebFinding.objects.select_related(
        'search_task__web_source',
        'search_task__search_term'
    ).all()

    total_db_records = all_findings.count()

    global_stats = {
        "total_db": total_db_records,
        "success": 0,
        "ignored_empty": 0,
        "ignored_stubs": 0,
        "unattempted": 0,
        "errors": 0
    }

    all_extracted_records = []

    if target_provider:
        key = target_provider.lower()
        if key not in PROVIDER_CONFIGS:
            logger.error(f"Error: Provider '{target_provider}' is not configured.")
            return [], global_stats
        active_configs = {key: PROVIDER_CONFIGS[key]}
    else:
        active_configs = PROVIDER_CONFIGS

    for prov_key, config in active_configs.items():
        keywords = config["keywords"]
        parser_func = config["parser"]

        query = Q()
        for kw in keywords:
            query |= Q(search_task__web_source__name__icontains=kw)

        prov_findings = all_findings.filter(query)
        results, stats = parser_func(prov_findings)
        all_extracted_records.extend(results)

        global_stats["success"] += stats["success"]
        global_stats["ignored_empty"] += stats["ignored_empty"]
        global_stats["ignored_stubs"] += stats["ignored_stubs"]
        global_stats["errors"] += stats["errors"]

    # Calculate unattempted records
    all_keywords = [kw for cfg in PROVIDER_CONFIGS.values() for kw in cfg["keywords"]]
    
    unattempted_query = Q(search_task__isnull=True) | Q(search_task__web_source__isnull=True)
    for kw in all_keywords:
        unattempted_query |= Q(search_task__web_source__name__icontains=kw) # Note: we adjust or count outside configured keywords

    # Simplified unattempted counter using database aggregation / iteration if needed
    unattempted_count = 0
    for finding in all_findings:
        task = finding.search_task
        if not task or not task.web_source:
            unattempted_count += 1
            continue

        source_name = task.web_source.name.lower()
        is_configured = any(kw in source_name for kw in all_keywords)

        if target_provider:
            target_keywords = PROVIDER_CONFIGS[target_provider.lower()]["keywords"]
            is_target = any(kw in source_name for kw in target_keywords)
            if not is_target:
                unattempted_count += 1
        else:
            if not is_configured:
                unattempted_count += 1

    global_stats["unattempted"] = unattempted_count

    return all_extracted_records, global_stats

def consolidate_extracted_records(raw_records):
    consolidated = {}

    for rec in raw_records:
        raw_ik = rec.get('inchikey')
        if not raw_ik:
            continue

        # Normalize InChIKey: strip prefix if present and uppercase
        ik = raw_ik.replace('InChIKey=', '').replace('inchikey=', '').strip()

        if ik not in consolidated:
            consolidated[ik] = {
                "inchikey": ik,
                "providers": set(),
                "search_hit_count": 0,
                "inchi": rec.get('inchi'),
                "canonical_smiles": rec.get('canonical_smiles'),
                "molecular_formula": rec.get('molecular_formula'),
                "molecular_weight": rec.get('molecular_weight'),
                "exact_mass": rec.get('exact_mass'),
                "preferred_name": rec.get('preferred_name'),
                "dtxsid": rec.get('dtxsid'),
            }

        group = consolidated[ik]
        group["search_hit_count"] += 1
        
        if rec.get('provider'):
            group["providers"].add(rec['provider'])

        # Merge fields (fill in blanks if subsequent providers have data)
        if not group["inchi"] and rec.get('inchi'):
            group["inchi"] = rec['inchi']
        if not group["canonical_smiles"] and rec.get('canonical_smiles'):
            group["canonical_smiles"] = rec['canonical_smiles']
        if not group["molecular_formula"] and rec.get('molecular_formula'):
            group["molecular_formula"] = rec['molecular_formula']
        if not group["molecular_weight"] and rec.get('molecular_weight'):
            group["molecular_weight"] = rec['molecular_weight']
        if not group["preferred_name"] and rec.get('preferred_name'):
            group["preferred_name"] = rec['preferred_name']
        if not group["dtxsid"] and rec.get('dtxsid'):
            group["dtxsid"] = rec['dtxsid']

    final_list = []
    for ik, group in consolidated.items():
        group["providers"] = sorted(list(group["providers"]))
        final_list.append(group)

    return final_list


def enrich_and_check_duplicates(extracted_records):
    """
    Checks consolidated records against the existing Structure table by InChIKey.
    """
    existing_inchikeys = set(
        Structure.objects.filter(
            inchikey__in=[r['inchikey'] for r in extracted_records if r.get('inchikey')]
        ).values_list('inchikey', flat=True)
    )

    enriched = []
    for rec in extracted_records:
        ik = rec.get('inchikey')
        rec['is_existing'] = ik in existing_inchikeys if ik else False
        enriched.append(rec)
        
    return enriched

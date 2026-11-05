import csv
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.db.models.functions import Lower, Upper
from django.utils import timezone

from apps.registry.models import (
    ChemicalActivity,
    Identifier,
    PhysChemProperty,
    SearchTask,
    SearchTerm,
    Structure,
    StructureComponent,
    StructureEvidence,
    Synonym,
    WebFinding,
)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_INPUT_ROWS = 20000
MAX_INPUT_COLUMNS = 100
UPLOAD_RETENTION = timedelta(days=1)
LOOKUP_TYPES = {
    'CAS': ('CAS', 'CAS-RN'),
    'DTXSID': ('DTXSID',),
    'PUBCHEM_CID': ('PUBCHEM_CID',),
    'CHEMSPIDER': ('CHEMSPIDER',),
    'UNII': ('UNII',),
    'EINECS': ('EINECS',),
    'OTHER': ('OTHER', 'MASKED_ID', 'CERAPP'),
}
SEARCH_TERM_TYPES = {
    'CAS': ('CAS', 'CASRN', 'CAS_RN', 'CAS-RN'),
    'NAME': (
        'NAME',
        'SYSTEMATIC',
        'IUPAC',
        'COMMON',
        'PREFERRED',
        'TRADE',
        'SYNONYM',
        'CHEMICAL_NAME',
    ),
}
IDENTIFIER_TYPES = (
    ('', 'Ignore this column'),
    ('CAS', 'CAS-RN'),
    ('DTXSID', 'EPA DTXSID'),
    ('PUBCHEM_CID', 'PubChem CID'),
    ('CHEMSPIDER', 'ChemSpider ID'),
    ('UNII', 'FDA UNII'),
    ('EINECS', 'EINECS / EC number'),
    ('OTHER', 'Other identifier'),
    ('NAME', 'Chemical name / synonym'),
    ('INCHIKEY', 'InChIKey'),
    ('INCHI', 'InChI'),
    ('SMILES', 'SMILES'),
)
DATA_SECTIONS = {'structure', 'identifiers', 'physchem', 'activities'}
RESOLUTION_MESSAGES = {
    'MATCHED': 'All usable inputs support the recommended structure.',
    'STEREO_VARIANT': (
        'The structures share connectivity but differ in stereochemistry.'
    ),
    'SALT_VARIANT': (
        'The structures resolve to the same parent after removing one or more '
        'non-parent fragments.'
    ),
    'PARENT_VARIANT': (
        'The structures resolve to the same parent after standardization.'
    ),
    'PARTIAL_MATCH': (
        'A structure is recommended from the inputs that matched; some other '
        'inputs did not resolve.'
    ),
    'CONFLICT': (
        'The inputs support structures with different connectivity. The '
        'recommended structure has the strongest evidence; alternatives are '
        'included.'
    ),
    'NOT_FOUND': 'No matching structure or database information was found.',
    'INVALID_INPUT': 'The supplied structure string could not be parsed.',
    'EMPTY_INPUT': 'No identifier values were provided for this row.',
}
RESOLUTION_LEVELS = {
    'MATCHED': '1 - Consistent structure',
    'STEREO_VARIANT': '2 - Stereochemistry differs',
    'SALT_VARIANT': '2 - Salt or fragment removed',
    'PARENT_VARIANT': '2 - Same standardized parent',
    'PARTIAL_MATCH': '3 - Partial evidence',
    'CONFLICT': '4 - Different structures; recommendation ranked by evidence',
    'NOT_FOUND': '5 - No CORE match',
    'INVALID_INPUT': '5 - Invalid structure input',
    'EMPTY_INPUT': '5 - No input provided',
}


def _upload_root():
    return Path(settings.BASE_DIR) / '.private' / 'chemical-lookups'


def _cleanup_old_uploads():
    root = _upload_root()
    if not root.exists():
        return
    cutoff = timezone.now().timestamp() - UPLOAD_RETENTION.total_seconds()
    for path in root.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()


def _write_upload(upload, extension):
    root = _upload_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    _cleanup_old_uploads()
    token = str(uuid.uuid4())
    destination = root / f'{token}{extension}'
    size = 0
    with destination.open('xb') as output:
        for chunk in upload.chunks():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                destination.unlink(missing_ok=True)
                raise ValueError('Files must be 10 MB or smaller.')
            output.write(chunk)
    return token, destination


def stage_uploaded_file(upload):
    filename = Path(upload.name).name
    extension = Path(filename).suffix.lower()
    if extension not in {'.csv', '.tsv', '.xlsx', '.xls'}:
        raise ValueError('Upload a CSV, TSV, or Excel file.')
    return _write_upload(upload, extension)


def stage_pasted_text(text, delimiter):
    if not text.strip():
        raise ValueError('Paste at least one chemical row.')
    if len(text.encode('utf-8')) > MAX_UPLOAD_BYTES:
        raise ValueError('Pasted input must be 10 MB or smaller.')
    if delimiter == 'auto':
        try:
            delimiter = csv.Sniffer().sniff(
                text[:4096],
                delimiters=',\t;',
            ).delimiter
        except csv.Error:
            delimiter = '\t' if '\t' in text else ','
    if delimiter not in {',', '\t', ';'}:
        raise ValueError('Choose comma, tab, semicolon, or automatic delimiter detection.')

    class TextUpload:
        name = 'pasted.csv'

        def chunks(self):
            yield text.encode('utf-8')

    token, path = _write_upload(TextUpload(), '.csv')
    return token, path, delimiter


def uploaded_path(token):
    try:
        parsed_token = uuid.UUID(str(token))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Your uploaded file is no longer available. Please start again.') from exc
    root = _upload_root().resolve()
    candidates = list(root.glob(f'{parsed_token}.*'))
    if len(candidates) != 1:
        raise ValueError('Your uploaded file is no longer available. Please start again.')
    path = candidates[0].resolve()
    if path.parent != root:
        raise ValueError('The selected upload path is invalid.')
    if path.stat().st_mtime < timezone.now().timestamp() - UPLOAD_RETENTION.total_seconds():
        path.unlink()
        raise ValueError('Your uploaded file expired. Please start again.')
    return path


def discard_upload(token):
    try:
        path = uploaded_path(token)
    except ValueError:
        return
    path.unlink(missing_ok=True)


def read_input_file(path, has_headers=True, delimiter='auto'):
    path = Path(path)
    extension = path.suffix.lower()
    try:
        if extension in {'.xlsx', '.xls'}:
            frame = pd.read_excel(
                path,
                sheet_name=0,
                dtype=str,
                header=0 if has_headers else None,
                nrows=MAX_INPUT_ROWS + 1,
            )
        else:
            encoding = 'utf-8-sig'
            if delimiter == 'auto':
                with path.open('r', encoding=encoding, newline='') as source:
                    sample = source.read(4096)
                try:
                    delimiter = csv.Sniffer().sniff(
                        sample,
                        delimiters=',\t;',
                    ).delimiter
                except csv.Error:
                    delimiter = '\t' if '\t' in sample else (
                        ';' if path.suffix.lower() == '.tsv' else ','
                    )
            frame = pd.read_csv(
                path,
                sep=delimiter,
                dtype=str,
                keep_default_na=False,
                encoding=encoding,
                header=0 if has_headers else None,
                nrows=MAX_INPUT_ROWS + 1,
                engine='python',
            )
    except Exception as exc:
        raise ValueError(
            'Could not read the file. Check the file format, delimiter, and header setting.'
        ) from exc

    if len(frame.columns) > MAX_INPUT_COLUMNS:
        raise ValueError(f'Files may contain no more than {MAX_INPUT_COLUMNS} columns.')
    if len(frame.index) > MAX_INPUT_ROWS:
        raise ValueError(f'Files may contain no more than {MAX_INPUT_ROWS} data rows.')

    if not has_headers:
        frame.columns = [
            f'Column {index + 1}' for index in range(len(frame.columns))
        ]
    headers = [str(column) for column in frame.columns]
    rows = [
        {
            f'column_{index}': (
                '' if pd.isna(value) else str(value).strip()
            )
            for index, value in enumerate(row)
        }
        for row in frame.itertuples(index=False, name=None)
    ]
    return headers, rows


def suggest_identifier_type(header):
    normalized = re.sub(r'[^a-z0-9]+', '', header.lower())
    if 'dtxsid' in normalized:
        return 'DTXSID'
    if 'cas' in normalized:
        return 'CAS'
    if 'inchikey' in normalized:
        return 'INCHIKEY'
    if 'inchi' in normalized:
        return 'INCHI'
    if 'smiles' in normalized or 'smile' in normalized:
        return 'SMILES'
    if 'pubchem' in normalized or normalized in {'cid', 'pubchemcid'}:
        return 'PUBCHEM_CID'
    if 'chemspider' in normalized:
        return 'CHEMSPIDER'
    if 'unii' in normalized:
        return 'UNII'
    if 'einecs' in normalized or normalized in {'ec', 'ecnumber'}:
        return 'EINECS'
    if 'name' in normalized or 'synonym' in normalized:
        return 'NAME'
    return ''


def _structure_key_from_representation(kind, value):
    try:
        from rdkit import Chem
    except ImportError as exc:
        raise RuntimeError('Structure-string lookup requires RDKit.') from exc

    if kind == 'INCHIKEY':
        key = value.strip().upper()
        return key if re.fullmatch(r'[A-Z]{14}-[A-Z]{10}-[A-Z]', key) else None
    if kind == 'SMILES':
        molecule = Chem.MolFromSmiles(value)
    else:
        molecule = Chem.MolFromInchi(value)
    if molecule is None:
        return None
    key = Chem.MolToInchiKey(molecule)
    return key.upper() if key else None


def _load_identifier_matches(rows, column_mapping):
    requested = {}
    for row in rows:
        for column, kind in column_mapping.items():
            value = str(row.get(f'column_{column}', '')).strip()
            if value and kind in {*LOOKUP_TYPES, 'NAME'}:
                requested.setdefault(kind, set()).add(value.casefold())

    matches = {}
    for kind, values in requested.items():
        if kind == 'NAME':
            continue
        identifier_types = LOOKUP_TYPES[kind]
        query = (
            Identifier.objects
            .annotate(normalized_value=Lower('identifier_value'))
            .filter(
                identifier_type__in=identifier_types,
                normalized_value__in=values,
            )
            .values_list(
                'identifier_type',
                'normalized_value',
                'identifier_value',
                'structures__inchikey',
            )
        )
        for identifier_type, normalized, actual, key in query.iterator():
            if key:
                matches.setdefault((kind, normalized), set()).add(key)

    name_values = requested.get('NAME', set())
    for normalized, actual, key in (
        Synonym.objects
        .annotate(normalized_value=Lower('synonym_value'))
        .filter(normalized_value__in=name_values)
        .values_list('normalized_value', 'synonym_value', 'structure__inchikey')
        .iterator()
    ):
        if key:
            matches.setdefault(('NAME', normalized), set()).add(key)
    return matches


def _load_search_term_evidence(rows, column_mapping):
    requested = {}
    for row in rows:
        for column, kind in column_mapping.items():
            value = str(row.get(f'column_{column}', '')).strip()
            if value and kind in SEARCH_TERM_TYPES:
                requested.setdefault(kind, set()).add(value.casefold())

    term_inputs = {}
    for kind, values in requested.items():
        terms = (
            SearchTerm.objects
            .annotate(normalized_value=Lower('term_value'))
            .annotate(normalized_type=Upper('term_type'))
            .filter(
                normalized_value__in=values,
                normalized_type__in=SEARCH_TERM_TYPES[kind],
            )
            .values_list('pk', 'normalized_value')
        )
        for term_id, normalized_value in terms.iterator():
            term_inputs[term_id] = (kind, normalized_value)

    evidence = {}
    if not term_inputs:
        return evidence

    for (
        term_id,
        structure_key,
        assessment,
        finding_id,
        task_id,
        provider_id,
    ) in (
        StructureEvidence.objects
        .filter(
            web_finding__search_task__search_term_id__in=term_inputs,
        )
        .values_list(
            'web_finding__search_task__search_term_id',
            'structure__inchikey',
            'assessment',
            'web_finding_id',
            'web_finding__search_task_id',
            'web_finding__search_task__web_source_id',
        )
        .iterator()
    ):
        kind, normalized_value = term_inputs[term_id]
        candidate = evidence.setdefault(
            (kind, normalized_value),
            {},
        ).setdefault(
            structure_key,
            {
                'supporting_findings': set(),
                'conflicting_findings': set(),
                'indeterminate_findings': set(),
                'conflicting_tasks': set(),
                'supporting_tasks': set(),
                'supporting_providers': set(),
            },
        )
        if assessment == StructureEvidence.Assessment.SUPPORTS:
            candidate['supporting_findings'].add(finding_id)
            candidate['supporting_tasks'].add(task_id)
            candidate['supporting_providers'].add(provider_id)
        elif assessment == StructureEvidence.Assessment.CONFLICTS:
            candidate['conflicting_findings'].add(finding_id)
            candidate['conflicting_tasks'].add(task_id)
        else:
            candidate['indeterminate_findings'].add(finding_id)

    chained_evidence = _load_provider_chain_evidence(term_inputs)
    for input_key, structure_evidence in chained_evidence.items():
        for structure_key, chain_values in structure_evidence.items():
            candidate = evidence.setdefault(input_key, {}).setdefault(
                structure_key,
                {
                    'supporting_findings': set(),
                    'conflicting_findings': set(),
                    'indeterminate_findings': set(),
                    'conflicting_tasks': set(),
                    'supporting_tasks': set(),
                    'supporting_providers': set(),
                },
            )
            for field, values in chain_values.items():
                candidate[field].update(values)

    return evidence


def _linked_provider_identifiers(provider_code, payload):
    if not isinstance(payload, dict):
        return ()
    if provider_code == 'pubchem':
        value = payload.get('resolved_cid')
        return (('CID', str(value)),) if value else ()
    if provider_code == 'comptox':
        value = payload.get('resolved_dtxsid')
        return (('DTXSID', str(value).strip().upper()),) if value else ()
    if provider_code == 'cas_common':
        value = payload.get('rn') or payload.get('casRN')
        return (('CAS', str(value).strip()),) if value else ()
    if provider_code == 'chembl':
        values = payload.get('resolved_chembl_ids') or ()
        if isinstance(values, str):
            values = [values]
        return tuple(
            ('CHEMBL_ID', str(value).strip().upper())
            for value in values
            if value
        )
    return ()


def _load_provider_chain_evidence(term_inputs):
    provider_details = {
        'pubchem': ('CID', 'pug_view'),
        'comptox': ('DTXSID', 'detail'),
        'cas_common': ('CAS', 'detail'),
        'chembl': ('CHEMBL_ID', 'default'),
    }
    if not term_inputs:
        return {}

    term_origins = {}
    for term_id, (kind, normalized_value) in term_inputs.items():
        term_origins.setdefault(term_id, set()).add((kind, normalized_value))

    root_paths = []
    for (
        source_id,
        provider_code,
        root_task_id,
        term_id,
        payload,
    ) in (
        WebFinding.objects
        .filter(
            search_task__search_term_id__in=term_origins,
        )
        .exclude(
            search_task__target_representation__in=('detail', 'pug_view'),
        )
        .values_list(
            'search_task__web_source_id',
            'search_task__web_source__provider_code',
            'search_task_id',
            'search_task__search_term_id',
            'raw_payload',
        )
        .iterator()
    ):
        if provider_code not in provider_details:
            continue
        detail_type, detail_representation = provider_details[provider_code]
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        identifiers = _linked_provider_identifiers(provider_code, decoded)
        if not identifiers:
            continue
        for input_key in term_origins[term_id]:
            for _linked_type, linked_value in identifiers:
                root_paths.append({
                    'input_key': input_key,
                    'source_id': source_id,
                    'provider_code': provider_code,
                    'root_task_id': root_task_id,
                    'detail_type': detail_type,
                    'detail_value': linked_value,
                    'detail_representation': detail_representation,
                })

    if not root_paths:
        return {}

    detail_lookup = {
        (
            path['detail_type'],
            path['detail_value'].casefold(),
            path['source_id'],
            path['detail_representation'],
        )
        for path in root_paths
    }
    detail_values = {value for _kind, value, _source, _representation in detail_lookup}
    detail_terms = {}
    for term_id, normalized_value in (
        SearchTerm.objects
        .annotate(normalized_value=Lower('term_value'))
        .filter(
            normalized_value__in=detail_values,
        )
        .values_list('pk', 'normalized_value')
        .iterator()
    ):
        detail_terms.setdefault(normalized_value, set()).add(term_id)

    root_by_detail = {}
    for path in root_paths:
        matching_term_ids = detail_terms.get(
            path['detail_value'].casefold(),
            set(),
        )
        for term_id in matching_term_ids:
            root_by_detail.setdefault(
                (
                    term_id,
                    path['source_id'],
                    path['detail_representation'],
                ),
                [],
            ).append(path)
    if not root_by_detail:
        return {}

    detail_tasks = {}
    for task_id, term_id, source_id, representation in (
        SearchTask.objects
        .filter(
            search_term_id__in={key[0] for key in root_by_detail},
            web_source_id__in={key[1] for key in root_by_detail},
            target_representation__in={
                key[2] for key in root_by_detail
            },
        )
        .values_list(
            'pk',
            'search_term_id',
            'web_source_id',
            'target_representation',
        )
        .iterator()
    ):
        paths = root_by_detail.get(
            (term_id, source_id, representation),
        )
        if paths:
            detail_tasks[task_id] = paths
    if not detail_tasks:
        return {}

    chained = {}
    for structure_key, assessment, finding_id, detail_task_id, provider_id in (
        StructureEvidence.objects
        .filter(web_finding__search_task_id__in=detail_tasks)
        .values_list(
            'structure__inchikey',
            'assessment',
            'web_finding_id',
            'web_finding__search_task_id',
            'web_finding__search_task__web_source_id',
        )
        .iterator()
    ):
        # Evidence from a provider detail lookup is credited to each distinct
        # input search task that resolved to that same provider identifier.
        for path in detail_tasks[detail_task_id]:
            candidate = chained.setdefault(
                path['input_key'],
                {},
            ).setdefault(
                structure_key,
                {
                    'supporting_findings': set(),
                    'conflicting_findings': set(),
                    'indeterminate_findings': set(),
                    'conflicting_tasks': set(),
                    'supporting_tasks': set(),
                    'supporting_providers': set(),
                },
            )
            if assessment == StructureEvidence.Assessment.SUPPORTS:
                candidate['supporting_findings'].add(finding_id)
                candidate['supporting_tasks'].add(path['root_task_id'])
                candidate['supporting_providers'].add(provider_id)
            elif assessment == StructureEvidence.Assessment.CONFLICTS:
                candidate['conflicting_findings'].add(finding_id)
                candidate['conflicting_tasks'].add(path['root_task_id'])
            else:
                candidate['indeterminate_findings'].add(finding_id)

    return chained


def _lookup_identifier_values(structure_keys):
    result = {key: [] for key in structure_keys}
    for identifier_type, identifier_value, key in (
        Identifier.objects
        .filter(structures__inchikey__in=structure_keys)
        .values_list(
            'identifier_type',
            'identifier_value',
            'structures__inchikey',
        )
        .order_by('identifier_type', 'identifier_value')
        .iterator()
    ):
        result[key].append({
            'type': identifier_type,
            'value': identifier_value,
        })
    for synonym_type, synonym_value, key in (
        Synonym.objects
        .filter(structure__inchikey__in=structure_keys)
        .values_list(
            'synonym_type',
            'synonym_value',
            'structure__inchikey',
        )
        .order_by('synonym_type', 'synonym_value')
        .iterator()
    ):
        result[key].append({
            'type': synonym_type,
            'value': synonym_value,
        })
    return result


def _lookup_properties(structure_keys):
    result = {key: [] for key in structure_keys}
    rows = (
        PhysChemProperty.objects
        .filter(structure__inchikey__in=structure_keys)
        .values(
            'structure__inchikey',
            'property_type',
            'numeric_value',
            'unit',
            'temperature_celsius',
            'ph',
            'determination_method',
            'source_reference',
        )
        .iterator()
    )
    for row in rows:
        key = row.pop('structure__inchikey')
        result[key].append(row)
    return result


def _lookup_activities(structure_keys):
    result = {key: [] for key in structure_keys}
    rows = (
        ChemicalActivity.objects
        .filter(structure__inchikey__in=structure_keys)
        .values(
            'structure__inchikey',
            'endpoint__code',
            'endpoint__name',
            'origin',
            'numeric_value',
            'unit',
            'qualitative_value',
            'source_reference',
            'notes',
        )
        .iterator()
    )
    for row in rows:
        key = row.pop('structure__inchikey')
        row['endpoint'] = {
            'code': row.pop('endpoint__code'),
            'name': row.pop('endpoint__name'),
        }
        result[key].append(row)
    return result


def resolve_chemical_rows(rows, column_mapping, data_sections):
    """Resolve input rows against the database and return a versioned API payload."""
    data_sections = set(data_sections)
    if data_sections - DATA_SECTIONS:
        raise ValueError('One or more requested data sections are not supported.')
    valid_columns = {str(index) for index in range(MAX_INPUT_COLUMNS)}
    if set(column_mapping) - valid_columns:
        raise ValueError('One or more mapped input columns are invalid.')
    allowed_types = {value for value, _label in IDENTIFIER_TYPES}
    if set(column_mapping.values()) - allowed_types:
        raise ValueError('One or more identifier column mappings are invalid.')

    identifier_matches = _load_identifier_matches(rows, column_mapping)
    search_term_evidence = _load_search_term_evidence(rows, column_mapping)
    result_rows = []
    structure_keys = set()
    for row_number, row in enumerate(rows, start=1):
        candidate_keys = set()
        candidate_evidence = {}
        unmatched_input_count = 0
        invalid_input_count = 0
        input_evidence = []
        for column, kind in column_mapping.items():
            value = str(row.get(f'column_{column}', '')).strip()
            if not value or not kind:
                continue
            if kind in {'INCHIKEY', 'INCHI', 'SMILES'}:
                key = _structure_key_from_representation(kind, value)
                keys = {key} if key else set()
                is_invalid = not keys
            else:
                keys = identifier_matches.get((kind, value.casefold()), set())
                is_invalid = False
            per_input_evidence = search_term_evidence.get(
                (kind, value.casefold()),
                {},
            )
            keys = set(keys).union(per_input_evidence)
            input_evidence.append({
                'keys': set(keys),
                'invalid': is_invalid,
                'source_evidence': per_input_evidence,
                'column': column,
                'kind': kind,
                'value': value,
            })
            if is_invalid:
                invalid_input_count += 1
            if keys:
                candidate_keys.update(keys)
                for candidate_key in keys:
                    evidence = candidate_evidence.setdefault(
                        candidate_key,
                        {
                            'input_count': 0,
                            'direct_input_count': 0,
                            'supporting_findings': set(),
                            'conflicting_findings': set(),
                            'indeterminate_findings': set(),
                            'supporting_tasks': set(),
                            'conflicting_tasks': set(),
                            'supporting_providers': set(),
                        },
                    )
                    evidence['input_count'] += 1
                    if kind in {'INCHIKEY', 'INCHI', 'SMILES'}:
                        evidence['direct_input_count'] += 1
                    for field in (
                        'supporting_findings',
                        'conflicting_findings',
                        'indeterminate_findings',
                        'conflicting_tasks',
                        'supporting_tasks',
                        'supporting_providers',
                    ):
                        evidence[field].update(
                            per_input_evidence
                            .get(candidate_key, {})
                            .get(field, set())
                        )

            else:
                if not is_invalid:
                    unmatched_input_count += 1

        structure_keys.update(candidate_keys)
        result_rows.append({
            'row_number': row_number,
            'input': dict(row),
            '_candidate_evidence': candidate_evidence,
            '_input_evidence': input_evidence,
            'resolution': {
                'matched_input_count': 0,
                'unmatched_input_count': unmatched_input_count,
                'invalid_input_count': invalid_input_count,
            },
            'structure': None,
            'alternatives': [],
            'data': {},
        })

    structures = {
        row['inchikey']: row
        for row in Structure.objects
        .filter(inchikey__in=structure_keys)
        .values(
            'inchikey',
            'inchi',
            'canonical_smiles',
            'isomeric_smiles',
            'molecular_formula',
            'molecular_weight',
            'exact_mass',
            'parent__inchikey',
        )
    }
    structures_with_removed_fragments = set(
        StructureComponent.objects
        .filter(
            structure__inchikey__in=structure_keys,
            is_parent_component=False,
        )
        .values_list('structure__inchikey', flat=True)
        .distinct()
    )
    identifiers = (
        _lookup_identifier_values(structure_keys)
        if 'identifiers' in data_sections
        else {}
    )
    physchem = (
        _lookup_properties(structure_keys)
        if 'physchem' in data_sections
        else {}
    )
    activities = (
        _lookup_activities(structure_keys)
        if 'activities' in data_sections
        else {}
    )
    for row in result_rows:
        evidence = row.pop('_candidate_evidence')
        input_evidence = row.pop('_input_evidence')
        known_candidate_keys = set(evidence).intersection(structures)
        row['unmatched_inputs'] = [
            {
                'column': item['column'],
                'type': item['kind'],
                'value': item['value'],
            }
            for item in input_evidence
            if not item['keys'].intersection(known_candidate_keys)
        ]
        resolution = row['resolution']
        parent_keys = {
            key: structures[key].get('parent__inchikey') or key
            for key in known_candidate_keys
        }
        for item in input_evidence:
            for candidate_key in item['keys'].intersection(known_candidate_keys):
                candidate_parent = parent_keys[candidate_key]
                candidate = evidence[candidate_key]
                for other_key, other_evidence in item['source_evidence'].items():
                    if (
                        other_key not in parent_keys
                        or parent_keys[other_key] == candidate_parent
                    ):
                        continue
                    candidate['conflicting_findings'].update(
                        other_evidence['supporting_findings']
                    )
                    candidate['conflicting_tasks'].update(
                        other_evidence['supporting_tasks']
                    )
        resolution['matched_input_count'] = sum(
            bool(item['keys'].intersection(known_candidate_keys))
            for item in input_evidence
        )
        resolution['unmatched_input_count'] = sum(
            not item['invalid']
            and not item['keys'].intersection(known_candidate_keys)
            for item in input_evidence
        )
        if known_candidate_keys:
            parent_groups = {}
            for key in known_candidate_keys:
                parent_groups.setdefault(parent_keys[key], []).append(key)
            group_evidence = {}
            for parent_key, group_keys in parent_groups.items():
                aggregated = {
                    'supporting_findings': set(),
                    'conflicting_findings': set(),
                    'indeterminate_findings': set(),
                    'supporting_tasks': set(),
                    'conflicting_tasks': set(),
                    'supporting_providers': set(),
                    'input_count': 0,
                    'direct_input_count': 0,
                }
                for key in group_keys:
                    for field in (
                        'supporting_findings',
                        'conflicting_findings',
                        'indeterminate_findings',
                        'supporting_tasks',
                        'conflicting_tasks',
                        'supporting_providers',
                    ):
                        aggregated[field].update(evidence[key][field])
                    aggregated['input_count'] += evidence[key]['input_count']
                    aggregated['direct_input_count'] += evidence[key]['direct_input_count']
                group_evidence[parent_key] = aggregated

            ranked_parents = sorted(
                parent_groups,
                key=lambda parent_key: (
                    -(
                        len(group_evidence[parent_key]['supporting_tasks'])
                        - len(group_evidence[parent_key]['conflicting_tasks'])
                    ),
                    -len(group_evidence[parent_key]['supporting_tasks']),
                    -len(group_evidence[parent_key]['supporting_providers']),
                    len(group_evidence[parent_key]['conflicting_tasks']),
                    -group_evidence[parent_key]['direct_input_count'],
                    -group_evidence[parent_key]['input_count'],
                    parent_key,
                ),
            )
            chosen_parent = ranked_parents[0]
            representative_keys = {}
            for parent_key, group_keys in parent_groups.items():
                representative_keys[parent_key] = sorted(
                    group_keys,
                    key=lambda key: (
                        -evidence[key]['direct_input_count'],
                        -len(evidence[key]['supporting_tasks']),
                        -len(evidence[key]['supporting_providers']),
                        -evidence[key]['input_count'],
                        key,
                    ),
                )[0]
            recommended_key = representative_keys[chosen_parent]
            has_unmatched = (
                resolution['unmatched_input_count']
                or resolution['invalid_input_count']
            )
            if len(parent_groups) > 1:
                status = 'CONFLICT'
            elif len(known_candidate_keys) > 1:
                connectivity_blocks = {
                    key[:14] for key in known_candidate_keys
                }
                has_removed_fragments = bool(
                    known_candidate_keys.intersection(
                        structures_with_removed_fragments
                    )
                )
                if len(connectivity_blocks) == 1:
                    status = 'STEREO_VARIANT'
                elif has_removed_fragments:
                    status = 'SALT_VARIANT'
                else:
                    status = 'PARENT_VARIANT'
            elif has_unmatched:
                status = 'PARTIAL_MATCH'
            else:
                status = 'MATCHED'

            recommended_evidence = group_evidence[chosen_parent]
            conflict_tasks = (
                recommended_evidence['conflicting_tasks']
                - recommended_evidence['supporting_tasks']
            )
            evidence_tasks = (
                recommended_evidence['supporting_tasks'] | conflict_tasks
            )
            resolution.update({
                'recommended_inchikey': recommended_key,
                'parent_inchikey': chosen_parent,
                'candidate_count': len(parent_groups),
                'supporting_finding_count': len(
                    recommended_evidence['supporting_findings']
                ),
                'conflicting_finding_count': len(
                    recommended_evidence['conflicting_findings']
                ),
                'supporting_search_task_count': len(
                    recommended_evidence['supporting_tasks']
                ),
                'conflicting_search_task_count': len(conflict_tasks),
                'supporting_provider_count': len(
                    recommended_evidence['supporting_providers']
                ),
                'evidence_task_count': len(evidence_tasks),
                'selection_tied': (
                    len(ranked_parents) > 1
                    and (
                        len(group_evidence[ranked_parents[0]]['supporting_tasks'])
                        - len(group_evidence[ranked_parents[0]]['conflicting_tasks'])
                    ) == (
                        len(group_evidence[ranked_parents[1]]['supporting_tasks'])
                        - len(group_evidence[ranked_parents[1]]['conflicting_tasks'])
                    )
                ),
            })
            if resolution['selection_tied']:
                resolution['message'] = (
                    'Evidence is tied between candidates; the first InChIKey '
                    'alphabetically is shown as a deterministic recommendation. '
                    + RESOLUTION_MESSAGES[status]
                )
            row['structure'] = dict(structures[recommended_key])
            row['structure']['parent__inchikey'] = chosen_parent
            row['alternatives'] = [
                representative_keys[parent_key]
                for parent_key in ranked_parents[1:]
            ]
        else:
            status = (
                'INVALID_INPUT'
                if resolution['invalid_input_count']
                and not resolution['unmatched_input_count']
                else (
                    'NOT_FOUND'
                    if resolution['unmatched_input_count']
                    else 'EMPTY_INPUT'
                )
            )
            resolution['recommended_inchikey'] = ''
            resolution['parent_inchikey'] = ''
            resolution['candidate_count'] = 0
            resolution['supporting_finding_count'] = 0
            resolution['conflicting_finding_count'] = 0
            resolution['supporting_search_task_count'] = 0
            resolution['conflicting_search_task_count'] = 0
            resolution['supporting_provider_count'] = 0
            resolution['evidence_task_count'] = 0
            resolution['selection_tied'] = False

        resolution['status'] = status
        support_count = resolution.get('supporting_search_task_count', 0)
        conflict_count = resolution.get('conflicting_search_task_count', 0)
        provider_count = resolution.get('supporting_provider_count', 0)
        resolution['confidence_level'] = (
            'HIGH'
            if support_count >= 3 and provider_count >= 2 and conflict_count == 0
            else (
                'MODERATE'
                if support_count >= 2 and support_count > conflict_count
                else 'LIMITED'
                if resolution['recommended_inchikey']
                else 'UNRESOLVED'
            )
        )
        if support_count:
            resolution['evidence_summary'] = (
                f'{support_count} of {resolution["evidence_task_count"]} tasks '
                f'from {provider_count} providers'
            )
            if conflict_count:
                resolution['evidence_summary'] += f'; {conflict_count} conflict'
                resolution['evidence_summary'] += (
                    's' if conflict_count != 1 else ''
                )
        else:
            resolution['evidence_summary'] = 'No reviewed structure evidence'
        if not resolution.get('message'):
            resolution['message'] = RESOLUTION_MESSAGES[status]
        resolution['level'] = RESOLUTION_LEVELS[status]

        key = resolution['recommended_inchikey']
        if key and 'identifiers' in data_sections:
            row['data']['identifiers'] = identifiers.get(key, [])
        if key and 'physchem' in data_sections:
            row['data']['physchem_properties'] = physchem.get(key, [])
        if key and 'activities' in data_sections:
            row['data']['activities'] = activities.get(key, [])

    return {
        'schema_version': '1.3',
        'data_sections': sorted(data_sections),
        'row_count': len(result_rows),
        'results': result_rows,
    }


def _safe_spreadsheet_text(value):
    if isinstance(value, (dict, list)):
        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(',', ':'),
        )
    elif value is None:
        return ''
    elif not isinstance(value, str):
        return value
    else:
        text = value
    if (
        text.startswith(('\t', '\r'))
        or text.lstrip(' \t\r\n').startswith(('=', '+', '-', '@'))
    ):
        return f"'{text}"
    return text


def export_rows(payload, headers, file_format):
    if file_format not in {'csv', 'xlsx'}:
        raise ValueError('Choose CSV or Excel as the download format.')

    input_headers = [
        f'input_{index + 1}_{header}'
        for index, header in enumerate(headers)
    ]
    export_headers = [
        'row_number',
        *input_headers,
        'resolution_status',
        'confidence_level',
        'evidence_summary',
        'recommended_inchikey',
        'alternative_inchikeys',
    ]
    if 'structure' in payload['data_sections']:
        export_headers.extend([
            'molecular_formula',
            'molecular_weight',
            'exact_mass',
            'canonical_smiles',
            'isomeric_smiles',
            'inchi',
            'parent_inchikey',
        ])
    for section, column in (
        ('identifiers', 'identifiers'),
        ('physchem', 'physchem_properties'),
        ('activities', 'activities'),
    ):
        if section in payload['data_sections']:
            export_headers.append(column)
    export_headers.append('admin_review_status')

    values = []
    for result in payload['results']:
        structure = result['structure'] or {}
        output = {
            'row_number': result['row_number'],
            'resolution_status': result['resolution']['status'],
            'admin_review_status': result.get('admin_review_status', ''),
            'confidence_level': result['resolution']['confidence_level'],
            'evidence_summary': result['resolution']['evidence_summary'],
            'recommended_inchikey': result['resolution']['recommended_inchikey'],
            'alternative_inchikeys': ', '.join(result['alternatives']),
        }
        output.update({
            export_header: result['input'].get(f'column_{index}', '')
            for index, export_header in enumerate(input_headers)
        })
        if 'structure' in payload['data_sections']:
            output.update({
                'molecular_formula': structure.get('molecular_formula', ''),
                'molecular_weight': structure.get('molecular_weight', ''),
                'exact_mass': structure.get('exact_mass', ''),
                'canonical_smiles': structure.get('canonical_smiles', ''),
                'isomeric_smiles': structure.get('isomeric_smiles', ''),
                'inchi': structure.get('inchi', ''),
                'parent_inchikey': structure.get('parent__inchikey', ''),
            })
        if 'identifiers' in payload['data_sections']:
            output['identifiers'] = result['data'].get('identifiers', [])
        if 'physchem' in payload['data_sections']:
            output['physchem_properties'] = result['data'].get(
                'physchem_properties',
                [],
            )
        if 'activities' in payload['data_sections']:
            output['activities'] = result['data'].get('activities', [])
        values.append({
            header: _safe_spreadsheet_text(output.get(header, ''))
            for header in export_headers
        })

    return export_headers, values


def export_collapsed_rows(payload, headers, collapse_mode, source_list_column=''):
    if collapse_mode not in {'exact', 'parent'}:
        raise ValueError('Choose exact-structure or parent-structure collapse.')
    valid_columns = {str(index) for index in range(len(headers))}
    if source_list_column and source_list_column not in valid_columns:
        raise ValueError('Choose a valid source-list column.')

    input_headers = [
        f'input_{index + 1}_{header}'
        for index, header in enumerate(headers)
    ]
    groups_by_key = {}
    for result in payload['results']:
        recommended_key = result['resolution']['recommended_inchikey']
        parent_key = result['resolution']['parent_inchikey']
        collapse_key = (
            (parent_key or recommended_key)
            if collapse_mode == 'parent'
            else recommended_key
        )
        grouping_key = (
            (collapse_mode, collapse_key)
            if collapse_key
            else ('unresolved-row', result['row_number'])
        )
        group = groups_by_key.setdefault(
            grouping_key,
            {
                'collapse_key': collapse_key,
                'members': [],
            },
        )
        group['members'].append(result)

    structure_keys = {
        group['collapse_key']
        for group in groups_by_key.values()
        if group['collapse_key']
    }
    structures = {
        row['inchikey']: row
        for row in Structure.objects
        .filter(inchikey__in=structure_keys)
        .values(
            'inchikey',
            'inchi',
            'canonical_smiles',
            'isomeric_smiles',
            'molecular_formula',
            'molecular_weight',
            'exact_mass',
            'parent__inchikey',
        )
    }
    data_sections = set(payload['data_sections'])
    identifiers = (
        _lookup_identifier_values(structure_keys)
        if 'identifiers' in data_sections
        else {}
    )
    physchem = (
        _lookup_properties(structure_keys)
        if 'physchem' in data_sections
        else {}
    )
    activities = (
        _lookup_activities(structure_keys)
        if 'activities' in data_sections
        else {}
    )

    collapsed_headers = [
        'collapsed_group_id',
        'collapse_mode',
        'collapse_key',
        'member_row_count',
        'source_lists',
        'rows_submitted_for_admin_review',
        'resolution_status_counts',
        'confidence_level_counts',
        'recommended_inchikey',
        'alternative_inchikeys',
    ]
    if 'structure' in data_sections:
        collapsed_headers.extend([
            'molecular_formula',
            'molecular_weight',
            'exact_mass',
            'canonical_smiles',
            'isomeric_smiles',
            'inchi',
            'parent_inchikey',
        ])
    for section, column in (
        ('identifiers', 'identifiers'),
        ('physchem', 'physchem_properties'),
        ('activities', 'activities'),
    ):
        if section in data_sections:
            collapsed_headers.append(column)

    lineage_headers = [
        'collapsed_group_id',
        'collapse_mode',
        'collapse_key',
        'source_list',
        'source_row_number',
        'resolution_status',
        'admin_review_status',
        'confidence_level',
        'evidence_summary',
        'recommended_inchikey',
        'parent_inchikey',
        'alternative_inchikeys',
        *input_headers,
    ]
    collapsed_rows = []
    lineage_rows = []
    mode_label = (
        'exact_structure' if collapse_mode == 'exact' else 'parent_structure'
    )
    for group in groups_by_key.values():
        members = group['members']
        collapse_key = group['collapse_key']
        group_id = (
            f'{collapse_mode.upper()}-{collapse_key}'
            if collapse_key
            else f'UNRESOLVED-ROW-{members[0]["row_number"]:06d}'
        )
        source_lists = []
        status_counts = {}
        confidence_counts = {}
        alternatives = set()
        for member in members:
            if source_list_column:
                source_list = str(
                    member['input'].get(f'column_{source_list_column}', '')
                ).strip() or 'Unlabeled source'
            else:
                source_list = 'Input file'
            if source_list not in source_lists:
                source_lists.append(source_list)
            status = member['resolution']['status']
            status_counts[status] = status_counts.get(status, 0) + 1
            confidence = member['resolution']['confidence_level']
            confidence_counts[confidence] = (
                confidence_counts.get(confidence, 0) + 1
            )
            if member.get('admin_review_status'):
                group['review_row_count'] = group.get('review_row_count', 0) + 1
            alternatives.update(member['alternatives'])

            lineage = {
                'collapsed_group_id': group_id,
                'collapse_mode': mode_label,
                'collapse_key': collapse_key,
                'source_list': source_list,
                'source_row_number': member['row_number'],
                'resolution_status': status,
                'admin_review_status': member.get('admin_review_status', ''),
                'confidence_level': confidence,
                'evidence_summary': member['resolution']['evidence_summary'],
                'recommended_inchikey': (
                    member['resolution']['recommended_inchikey']
                ),
                'parent_inchikey': member['resolution']['parent_inchikey'],
                'alternative_inchikeys': ', '.join(member['alternatives']),
            }
            lineage.update({
                export_header: member['input'].get(f'column_{column}', '')
                for column, export_header in enumerate(input_headers)
            })
            lineage_rows.append({
                header: _safe_spreadsheet_text(lineage.get(header, ''))
                for header in lineage_headers
            })

        target_key = collapse_key or ''
        structure = structures.get(target_key, {}) if target_key else {}
        group_row = {
            'collapsed_group_id': group_id,
            'collapse_mode': mode_label,
            'collapse_key': collapse_key,
            'member_row_count': len(members),
            'source_lists': source_lists,
            'rows_submitted_for_admin_review': group.get('review_row_count', 0),
            'resolution_status_counts': status_counts,
            'confidence_level_counts': confidence_counts,
            'recommended_inchikey': target_key,
            'alternative_inchikeys': ', '.join(
                sorted(alternatives - {target_key})
            ),
        }
        if 'structure' in data_sections:
            group_row.update({
                'molecular_formula': structure.get('molecular_formula', ''),
                'molecular_weight': structure.get('molecular_weight', ''),
                'exact_mass': structure.get('exact_mass', ''),
                'canonical_smiles': structure.get('canonical_smiles', ''),
                'isomeric_smiles': structure.get('isomeric_smiles', ''),
                'inchi': structure.get('inchi', ''),
                'parent_inchikey': structure.get('parent__inchikey', ''),
            })
        if 'identifiers' in data_sections:
            group_row['identifiers'] = identifiers.get(target_key, [])
        if 'physchem' in data_sections:
            group_row['physchem_properties'] = physchem.get(target_key, [])
        if 'activities' in data_sections:
            group_row['activities'] = activities.get(target_key, [])
        collapsed_rows.append({
            header: _safe_spreadsheet_text(group_row.get(header, ''))
            for header in collapsed_headers
        })

    return collapsed_headers, collapsed_rows, lineage_headers, lineage_rows

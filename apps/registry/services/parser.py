import csv
import hashlib
import json
import os
import traceback
import pandas as pd
import tempfile
from django.conf import settings
from django.db.models import Q
from django.db import transaction
from ..models import (
    ChemicalGroup,
    Identifier,
    RawSourceRow,
    RawSubstanceInput,
    SearchTask,
    SearchTerm,
    Source,
    SourceImport,
    Structure,
    WebSource,
)
from .cas_validator import detect_and_suggest_cas
from .task_generator import (
    _is_masked_identifier_type,
    _is_valid_source_pairing,
    _representations_for_source,
)


def parse_and_stage_file(file_obj):
    original_filename = os.path.basename(file_obj.name)
    ext = os.path.splitext(original_filename)[1].lower()

    if ext not in ('.csv', '.xlsx', '.xls'):
        raise ValueError('Please upload a CSV or Excel file.')

    upload_dir = os.path.join(settings.MEDIA_ROOT, 'imports')
    os.makedirs(upload_dir, exist_ok=True)

    digest = hashlib.sha256()
    temp_fd, temp_path = tempfile.mkstemp(
        prefix='.upload-',
        dir=upload_dir,
    )

    try:
        with os.fdopen(temp_fd, 'wb') as destination:
            for chunk in file_obj.chunks():
                digest.update(chunk)
                destination.write(chunk)

        file_hash = digest.hexdigest()

        duplicate_exists = SourceImport.objects.filter(
            file_hash=file_hash,
            import_status__in=(
                SourceImport.ImportStatus.PENDING,
                SourceImport.ImportStatus.PROCESSING,
                SourceImport.ImportStatus.SUCCESS,
            ),
        ).exists()

        if duplicate_exists:
            raise ValueError(
                'This exact file has already been imported '
                '(duplicate hash detected).'
            )

        file_path = os.path.join(upload_dir, f'{file_hash}{ext}')

        # A failed import may be retried with the same file. Keep its existing
        # content-addressed file rather than replacing it unnecessarily.
        if os.path.exists(file_path):
            os.remove(temp_path)
        else:
            os.replace(temp_path, file_path)

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    if ext == '.xlsx':
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(
                file_path,
                read_only=True,
                data_only=True,
            )
            try:
                worksheet = workbook.worksheets[0]
                first_row = next(
                    worksheet.iter_rows(values_only=True),
                    None,
                )
                headers = [
                    str(value) if value is not None else ''
                    for value in (first_row or ())
                ]
            finally:
                workbook.close()

            used_encoding = 'excel'
        except Exception as exc:
            raise ValueError(
                f'Could not parse Excel workbook: {exc}'
            ) from exc

    elif ext == '.xls':
        try:
            # This reads the header without building a DataFrame of all rows.
            df = pd.read_excel(
                file_path,
                sheet_name=0,
                nrows=0,
                dtype=str,
            )
            headers = list(df.columns)
            used_encoding = 'excel'
        except Exception as exc:
            raise ValueError(
                f'Could not parse Excel workbook: {exc}'
            ) from exc

    else:
        headers = None
        used_encoding = None

        for encoding in ('utf-8-sig', 'latin-1', 'cp1252'):
            try:
                with open(
                    file_path,
                    'r',
                    encoding=encoding,
                    newline='',
                ) as source_file:
                    headers = next(csv.reader(source_file), [])
                used_encoding = encoding
                break
            except UnicodeDecodeError:
                continue

        if headers is None:
            raise ValueError(
                'Could not decode the CSV file. '
                'Please save it using a supported text encoding.'
            )

    return file_path, file_hash, headers, used_encoding, original_filename


def get_csv_preview_rows(file_path, encoding, limit=5):
    preview_rows = []
    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext in ['.xlsx', '.xls'] or encoding == 'excel':
            df = pd.read_excel(file_path, sheet_name=0, dtype=str).fillna('')
            for idx, row in df.head(limit).iterrows():
                preview_rows.append(row.tolist())
        else:
            read_encoding = encoding if encoding and encoding not in ('excel', 'excel-dataframe', 'utf-8', 'utf-8-sig') else 'latin-1'
            with open(file_path, 'r', encoding=read_encoding, errors='replace') as f:
                reader = csv.reader(f)
                next(reader, None)
                for idx, row in enumerate(reader):
                    if idx >= limit:
                        break
                    preview_rows.append(row)
    except Exception as e:
        traceback.print_exc()

    return preview_rows


def iter_source_rows(file_path, encoding):
    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.csv':
        with open(file_path, encoding=encoding, newline='') as source_file:
            reader = csv.DictReader(source_file)
            for row_index, row in enumerate(reader, start=1):
                yield row_index, row
        return

    if ext == '.xlsx':
        from openpyxl import load_workbook

        workbook = load_workbook(
            file_path,
            read_only=True,
            data_only=True,
        )
        try:
            worksheet = workbook.worksheets[0]
            rows = worksheet.iter_rows(values_only=True)
            headers = next(rows, None)

            if headers is None:
                return

            headers = [
                str(value) if value is not None else ''
                for value in headers
            ]

            for row_index, values in enumerate(rows, start=1):
                yield row_index, dict(zip(headers, values))
        finally:
            workbook.close()
        return

    if ext == '.xls':
        # Legacy .xls parsing uses pandas and loads the workbook into memory.
        # Avoid using this path for very large files; convert those to .xlsx.
        dataframe = pd.read_excel(file_path, sheet_name=0, dtype=str).fillna('')
        for row_index, row in enumerate(
            dataframe.to_dict(orient='records'),
            start=1,
        ):
            yield row_index, row
        return

    raise ValueError(f'Unsupported import file type: {ext}')


def generate_staged_preview(file_path, encoding, mapping):
    """Parses file rows, bulk-checks existing terms, validates CAS/HCIDs, and sorts results instantly."""
    staged_rows = []
    ext = os.path.splitext(file_path)[1].lower()
    reader_dicts = iter_source_rows(file_path, encoding)

    # OPTIMIZATION: Pre-load all existing SearchTerm values into a set in 1 query
    existing_terms_set = set(SearchTerm.objects.values_list('term_value', flat=True))
    existing_masked_ids_set = set(
        Identifier.objects.filter(
            identifier_type=Identifier.IdentifierType.MASKED_ID,
        ).values_list('identifier_value', flat=True)
    )

    for idx, row in reader_dicts:
        primary_id = None
        primary_type = "Unspecified"
        structure_anchors = []
        cas_validation = None
        attributes_summary = []

        for csv_header, map_info in mapping.items():
            cell_value = str(row.get(csv_header, '')).strip()
            if not cell_value:
                continue

            cat = map_info.get('category')
            sub = map_info.get('subtype', cat)

            if cat == 'IDENTIFIER':
                if sub == 'CAS' or 'CAS' in csv_header.upper() or 'HCID' in csv_header.upper():
                    cas_validation = detect_and_suggest_cas(cell_value)
                    if cas_validation and cas_validation.get('suggestion'):
                        primary_id = cas_validation['suggestion']  # Immediately use suggestion
                        primary_type = 'CAS'
                    else:
                        primary_id = cell_value
                        primary_type = sub
                elif not primary_id:
                    primary_id = cell_value
                    primary_type = sub
                else:
                    attributes_summary.append(f"{sub}: {cell_value}")

            elif cat == 'SYNONYM':
                if not primary_id:
                    primary_id = cell_value
                    primary_type = sub
                else:
                    attributes_summary.append(f"{sub}: {cell_value}")
            elif cat in ('SMILES_CANONICAL', 'SMILES_ISOMERIC', 'INCHIKEY', 'INCHI'):
                structure_anchors.append((cell_value, sub))
            elif cat in ('PHYSCHEM', 'ENDPOINT'):
                attributes_summary.append(f"{sub}: {cell_value}")

        if not primary_id and structure_anchors:
            primary_id = structure_anchors[0][0][:27]
            primary_type = structure_anchors[0][1]

        # CAS Validation & Silent Auto-Normalization
        is_cas_valid = True
        has_cas_warning = False

        if cas_validation:
            if cas_validation.get('is_valid'):
                is_cas_valid = True
                has_cas_warning = False
            elif cas_validation.get('suggestion'):
                primary_id = cas_validation['suggestion']
                primary_type = 'CAS'
                is_cas_valid = True
                has_cas_warning = False
            else:
                is_cas_valid = False
                has_cas_warning = True

        is_existing_term = False
        if primary_id and primary_id != "[Missing Identifier]":
            if _is_masked_identifier_type(primary_type):
                is_existing_term = primary_id in existing_masked_ids_set
            else:
                is_existing_term = primary_id in existing_terms_set

        sort_priority = 1
        if has_cas_warning or not is_cas_valid:
            sort_priority = 0
        elif is_existing_term:
            sort_priority = 2

        staged_rows.append({
            'row_index': idx,
            'primary_id': primary_id or "[Missing Identifier]",
            'primary_type': primary_type,
            'has_structure': bool(structure_anchors),
            'cas_validation': cas_validation,
            'attributes_summary': attributes_summary,
            'raw_json': json.dumps(row),
            'is_valid': bool(primary_id or structure_anchors) and is_cas_valid,
            'is_existing': is_existing_term,
            'has_cas_warning': has_cas_warning,
            'sort_priority': sort_priority
        })

    staged_rows.sort(key=lambda x: (x['sort_priority'], x['row_index']))
    return staged_rows


def _propagate_tasks_for_term(search_term):
    active_sources = WebSource.objects.filter(is_active=True)
    for source in active_sources:
        source_identifier = (getattr(source, 'provider_code', None) or source.name).lower()
        representations = _representations_for_source(
            search_term,
            source_identifier,
        )
        for rep in representations:
            SearchTask.objects.get_or_create(
                search_term=search_term,
                web_source=source,
                target_representation=rep,
                defaults={'status': SearchTask.TaskStatus.PENDING}
            )


@transaction.atomic
def commit_parsed_import(
    file_hash,
    file_path,
    original_filename,
    staged_rows,
    mapping=None,
    group_name=None,
    source_import=None,
):
    source, _ = Source.objects.get_or_create(
        source_name=original_filename,
        defaults={
            'source_description': 'Imported via management wizard (CSV/Excel). Archived on disk.',
            'original_file_path': os.path.abspath(file_path)
        }
    )

    if not source.original_file_path:
        source.original_file_path = os.path.abspath(file_path)
        source.save(update_fields=['original_file_path'])

    target_group = None
    if group_name and group_name.strip():
        target_group, _ = ChemicalGroup.objects.get_or_create(
            name=group_name.strip(),
            defaults={'description': f"Created via import of {original_filename}"}
        )

    if source_import is None:
        source_import = SourceImport.objects.create(
            source=source,
            file_hash=file_hash,
            row_count=len(staged_rows),
            chemical_group=target_group,
            import_status=SourceImport.ImportStatus.PENDING,
        )
    else:
        source_import.source = source
        source_import.chemical_group = target_group

    searchable_categories = ('IDENTIFIER', 'SYNONYM', 'SMILES_CANONICAL', 'SMILES_ISOMERIC', 'INCHIKEY', 'INCHI')

    with transaction.atomic():
        raw_row_objs = [
            RawSourceRow(
                source_import=source_import,
                row_index=item['row_index'],
                original_json_dump=item['raw_json']
            )
            for item in staged_rows
        ]
        created_raw_rows = RawSourceRow.objects.bulk_create(raw_row_objs, batch_size=2000)
        row_index_map = {r.row_index: r for r in created_raw_rows}

        terms_to_process = set()
        row_input_pairs = []

        for item in staged_rows:
            idx = item['row_index']
            raw_row = row_index_map.get(idx)
            if not raw_row:
                continue

            final_id = item['primary_id']
            final_type = item['primary_type']

            cas_val = item.get('cas_validation')
            if cas_val and cas_val.get('suggestion'):
                final_id = cas_val['suggestion']
                final_type = 'CAS'
            elif cas_val and not cas_val.get('is_valid') and not cas_val.get('suggestion'):
                final_id = "[Missing Identifier]"

            raw_json_str = item['raw_json']
            raw_dict = {}
            try:
                raw_dict = json.loads(raw_json_str)
            except Exception:
                pass

            seen_pairs = set()

            if final_id and final_id != "[Missing Identifier]":
                pair = (final_id, final_type)
                if pair not in seen_pairs:
                    if not _is_masked_identifier_type(final_type):
                        terms_to_process.add(pair)
                    row_input_pairs.append((raw_row, final_id, final_type))
                    seen_pairs.add(pair)

            if mapping and raw_dict:
                for csv_header, map_info in mapping.items():
                    cat = map_info.get('category')
                    sub = map_info.get('subtype', cat)
                    val = str(raw_dict.get(csv_header, '')).strip()

                    if not val:
                        continue

                    if sub == 'CAS' or 'CAS' in csv_header.upper() or 'HCID' in csv_header.upper():
                        cas_check = detect_and_suggest_cas(val)
                        if cas_check and cas_check.get('suggestion'):
                            val = cas_check['suggestion']
                            sub = 'CAS'
                        elif cas_check and not cas_check.get('is_valid') and not cas_check.get('suggestion'):
                            continue

                    if cat in searchable_categories:
                        pair = (val, sub)
                        if pair not in seen_pairs:
                            if not _is_masked_identifier_type(sub):
                                terms_to_process.add(pair)
                            row_input_pairs.append((raw_row, val, sub))
                            seen_pairs.add(pair)

        term_values = [val for val, _ in terms_to_process]
        existing_terms = {
            t.term_value: t
            for t in SearchTerm.objects.filter(term_value__in=term_values)
        }

        unique_terms_map = {}
        for val, itype in terms_to_process:
            if val not in unique_terms_map:
                unique_terms_map[val] = itype
            elif 'CAS' in itype and 'CAS' not in unique_terms_map[val]:
                unique_terms_map[val] = itype

        new_term_objs = []
        for val, itype in unique_terms_map.items():
            if val not in existing_terms:
                new_term_objs.append(SearchTerm(term_value=val, term_type=itype, frequency_count=1))

        if new_term_objs:
            SearchTerm.objects.bulk_create(new_term_objs, ignore_conflicts=True, batch_size=2000)
            all_vals = list(unique_terms_map.keys())
            existing_terms = {
                t.term_value: t
                for t in SearchTerm.objects.filter(term_value__in=all_vals)
            }

        masked_identifier_values = {
            value
            for _, value, input_type in row_input_pairs
            if _is_masked_identifier_type(input_type)
        }
        existing_identifiers = {
            identifier.identifier_value: identifier
            for identifier in Identifier.objects.filter(
                identifier_type=Identifier.IdentifierType.MASKED_ID,
                identifier_value__in=masked_identifier_values,
            )
        }
        new_identifiers = [
            Identifier(
                identifier_type=Identifier.IdentifierType.MASKED_ID,
                identifier_value=value,
            )
            for value in masked_identifier_values
            if value not in existing_identifiers
        ]
        if new_identifiers:
            Identifier.objects.bulk_create(
                new_identifiers,
                ignore_conflicts=True,
                batch_size=2000,
            )
            existing_identifiers.update({
                identifier.identifier_value: identifier
                for identifier in Identifier.objects.filter(
                    identifier_type=Identifier.IdentifierType.MASKED_ID,
                    identifier_value__in=masked_identifier_values,
                )
            })

        if target_group and existing_terms:
            ThroughModel = SearchTerm.groups.through
            through_objs = [
                ThroughModel(searchterm=t, chemicalgroup=target_group)
                for t in existing_terms.values()
            ]
            ThroughModel.objects.bulk_create(through_objs, ignore_conflicts=True, batch_size=2000)

        inputs_to_create = [
            RawSubstanceInput(
                raw_row=raw_row,
                input_type=itype,
                input_value=val,
                search_term=(
                    None if _is_masked_identifier_type(itype)
                    else existing_terms.get(val)
                ),
                identifier=(
                    existing_identifiers.get(val)
                    if _is_masked_identifier_type(itype)
                    else None
                ),
                is_processed=True
            )
            for raw_row, val, itype in row_input_pairs
            if (
                _is_masked_identifier_type(itype)
                or val in existing_terms
            )
        ]

        RawSubstanceInput.objects.bulk_create(inputs_to_create, batch_size=2000)

    active_sources = list(WebSource.objects.filter(is_active=True))

    if active_sources and existing_terms:
        tasks_to_create = []

        for term in existing_terms.values():
            for web_source in active_sources:
                provider_code = (web_source.provider_code or web_source.name).lower()

                if not _is_valid_source_pairing(term, provider_code):
                    continue

                representations = _representations_for_source(
                    term,
                    provider_code,
                )

                for rep in representations:
                    tasks_to_create.append(
                        SearchTask(
                            search_term=term,
                            web_source=web_source,
                            target_representation=rep,
                            status='PENDING',
                            is_completed=False
                        )
                    )

        if tasks_to_create:
            SearchTask.objects.bulk_create(tasks_to_create, ignore_conflicts=True, batch_size=2000)

    source_import.row_count = len(staged_rows)
    source_import.import_status = SourceImport.ImportStatus.SUCCESS
    source_import.error_log = ''
    source_import.save(
        update_fields=['chemical_group', 'row_count', 'import_status', 'error_log']
    )

    return source_import

def iter_staged_preview_chunks(file_path, encoding, mapping, chunk_size=1000):
    if chunk_size < 1:
        raise ValueError('chunk_size must be greater than zero.')

    def finalize_chunk(rows):
        term_values = {
            row['primary_id']
            for row in rows
            if row['primary_id'] != '[Missing Identifier]'
        }
        existing_terms = set(
            SearchTerm.objects.filter(
                term_value__in=term_values
            ).values_list('term_value', flat=True)
        )
        masked_identifier_values = {
            row['primary_id']
            for row in rows
            if (
                _is_masked_identifier_type(row['primary_type'])
                and row['primary_id'] != '[Missing Identifier]'
            )
        }
        existing_masked_ids = set(
            Identifier.objects.filter(
                identifier_type=Identifier.IdentifierType.MASKED_ID,
                identifier_value__in=masked_identifier_values,
            ).values_list('identifier_value', flat=True)
        )

        for row in rows:
            if _is_masked_identifier_type(row['primary_type']):
                row['is_existing'] = row['primary_id'] in existing_masked_ids
            else:
                row['is_existing'] = row['primary_id'] in existing_terms
            if row['has_cas_warning'] or not row['is_valid']:
                row['sort_priority'] = 0
            elif row['is_existing']:
                row['sort_priority'] = 2
            else:
                row['sort_priority'] = 1

        rows.sort(key=lambda row: (row['sort_priority'], row['row_index']))
        return rows

    chunk = []

    for row_index, row in iter_source_rows(file_path, encoding):
        primary_id = None
        primary_type = 'Unspecified'
        structure_anchors = []
        cas_validation = None
        attributes_summary = []

        for header, map_info in mapping.items():
            cell_value = str(row.get(header, '') or '').strip()
            if not cell_value:
                continue

            category = map_info.get('category')
            subtype = map_info.get('subtype', category)

            if category == 'IDENTIFIER':
                if (
                    subtype == 'CAS'
                    or 'CAS' in header.upper()
                    or 'HCID' in header.upper()
                ):
                    cas_validation = detect_and_suggest_cas(cell_value)
                    if cas_validation and cas_validation.get('suggestion'):
                        primary_id = cas_validation['suggestion']
                        primary_type = 'CAS'
                    else:
                        primary_id = cell_value
                        primary_type = subtype
                elif not primary_id:
                    primary_id = cell_value
                    primary_type = subtype
                else:
                    attributes_summary.append(f'{subtype}: {cell_value}')

            elif category == 'SYNONYM':
                if not primary_id:
                    primary_id = cell_value
                    primary_type = subtype
                else:
                    attributes_summary.append(f'{subtype}: {cell_value}')

            elif category in (
                'SMILES_CANONICAL',
                'SMILES_ISOMERIC',
                'INCHIKEY',
                'INCHI',
            ):
                structure_anchors.append((cell_value, subtype))

            elif category in ('PHYSCHEM', 'ENDPOINT'):
                attributes_summary.append(f'{subtype}: {cell_value}')

        if not primary_id and structure_anchors:
            primary_id = structure_anchors[0][0][:27]
            primary_type = structure_anchors[0][1]

        is_cas_valid = True
        has_cas_warning = False

        if cas_validation:
            if cas_validation.get('is_valid'):
                pass
            elif cas_validation.get('suggestion'):
                primary_id = cas_validation['suggestion']
                primary_type = 'CAS'
            else:
                is_cas_valid = False
                has_cas_warning = True

        chunk.append({
            'row_index': row_index,
            'primary_id': primary_id or '[Missing Identifier]',
            'primary_type': primary_type,
            'has_structure': bool(structure_anchors),
            'cas_validation': cas_validation,
            'attributes_summary': attributes_summary,
            'raw_json': json.dumps(row, default=str),
            'is_valid': bool(primary_id or structure_anchors) and is_cas_valid,
            'is_existing': False,
            'has_cas_warning': has_cas_warning,
            'sort_priority': 1,
        })

        if len(chunk) >= chunk_size:
            yield finalize_chunk(chunk)
            chunk = []

    if chunk:
        yield finalize_chunk(chunk)
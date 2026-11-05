import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.registry.models import (
    Structure,
    StructureComponent,
    StructureCurationBatch,
    StructureCurationBatchFinding,
    StructureCurationQueueCursor,
    StructureEvidence,
    WebFinding,
)
from apps.registry.services.extractor import PROVIDER_CONFIGS
from apps.registry.services.identifier_linking import (
    get_search_term_ids_for_findings,
    link_verified_identifiers,
)


CHUNK_SIZE = 2000
PARTITION_COUNT = 128
OVERLAP = timedelta(minutes=5)
REPORT_FORMAT_VERSION = 25
MASS_RELATIVE_TOLERANCE = 0.005
MAX_FINDING_BATCH_SIZE = 50000
INCHIKEY_PATTERN = re.compile(r'^[A-Z]{14}-[A-Z]{10}-[A-Z]$')
FORMULA_ELEMENT_PATTERN = re.compile(r'[A-Z][a-z]?')
COUNTERION_ELEMENTS = frozenset({
    'Li', 'Na', 'K', 'Rb', 'Cs', 'Mg', 'Ca', 'Sr', 'Ba',
})
FORMULA_ELEMENTS = frozenset(
    'H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn '
    'Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La '
    'Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po '
    'At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg '
    'Cn Nh Fl Mc Lv Ts Og'.split()
)
FORMULA_ATOM_SYMBOLS = FORMULA_ELEMENTS | {'D', 'T'}
FORMULA_SUBSCRIPTS = str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789')
PROVIDER_ALIASES = {'cas_common': 'cas'}


def _root():
    return Path(
        getattr(
            settings,
            'STRUCTURE_CURATION_ROOT',
            Path(settings.BASE_DIR) / '.private' / 'structure-curation',
        )
    )


@contextmanager
def _root_lock():
    import fcntl

    root = _root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    with (root / '.run.lock').open('a+') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield root
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(run_id):
    import fcntl

    run_dir = _run_dir(run_id)
    with (run_dir / '.worker.lock').open('a+') as lock_file:
        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise RuntimeError('This structure scan is already running.') from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _run_dir(run_id):
    return _root() / str(uuid.UUID(str(run_id)))


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        dir=path.parent,
        prefix=f'.{path.name}.',
        suffix='.tmp',
        delete=False,
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = temporary.name
    os.replace(temporary_path, path)


def read_run_state(run_id):
    state_path = _run_dir(run_id) / 'state.json'
    with state_path.open(encoding='utf-8') as state_file:
        return json.load(state_file)


def _write_state(run_id, state):
    _atomic_json(_run_dir(run_id) / 'state.json', state)


def _read_checkpoint():
    checkpoint_path = _root() / 'checkpoint.json'
    if not checkpoint_path.exists():
        return None
    with checkpoint_path.open(encoding='utf-8') as checkpoint_file:
        return json.load(checkpoint_file)


def create_run(run_id, full_scan=False):
    run_id = str(uuid.UUID(str(run_id)))
    with _root_lock() as root:
        active_path = root / 'active_run.json'
        if active_path.exists():
            active = json.loads(active_path.read_text(encoding='utf-8'))
            try:
                active_state = read_run_state(active['id'])
            except (OSError, ValueError, json.JSONDecodeError):
                active_state = {}
            if active_state.get('status') in {
                'QUEUED', 'RUNNING', 'FINALIZING',
            }:
                raise RuntimeError(
                    f"Structure scan {active['id']} is already active."
                )
            active_path.unlink(missing_ok=True)

        checkpoint = None if full_scan else _read_checkpoint()
        start_after = None
        if checkpoint:
            checkpoint_time = datetime.fromisoformat(checkpoint['updated_at'])
            start_after = (checkpoint_time - OVERLAP).isoformat()

        now = timezone.now()
        state = {
            'id': run_id,
            'status': 'QUEUED',
            'scan_mode': 'full' if full_scan else 'incremental',
            'expected_report_format_version': REPORT_FORMAT_VERSION,
            'created_at': now.isoformat(),
            'updated_at': now.isoformat(),
            'upper_bound': now.isoformat(),
            'start_after': start_after,
            'cursor_updated_at': None,
            'cursor_id': None,
            'page_number': 0,
            'processed_findings': 0,
            'total_findings': None,
            'unique_keys': 0,
            'stats': {
                'missing_key': 0,
                'invalid_key': 0,
                'derived_keys': 0,
                'ambiguous_structure': 0,
                'parser_errors': 0,
                'unsupported_provider': 0,
            },
            'error': None,
        }
        run_dir = _run_dir(run_id)
        run_dir.mkdir(mode=0o700)
        _write_state(run_id, state)
        _atomic_json(root / 'latest_run.json', {'id': run_id})
        _atomic_json(active_path, {'id': run_id})
        return state


def prepare_finding_batch(run_id, batch_limit=1000, full_scan=False):
    run_id = str(uuid.UUID(str(run_id)))
    if not full_scan:
        try:
            batch_limit = int(batch_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError('Enter a positive WebFinding batch size.') from exc
        if not 1 <= batch_limit <= MAX_FINDING_BATCH_SIZE:
            raise ValueError(
                f'Batch size must be between 1 and {MAX_FINDING_BATCH_SIZE}.'
            )

    with _root_lock(), transaction.atomic():
        if StructureCurationBatch.objects.filter(run_id=run_id).exists():
            raise ValueError('A WebFinding batch already exists for this scan.')
        upper_bound = timezone.now()
        cursor, _created = StructureCurationQueueCursor.objects.get_or_create(
            pk=1,
        )
        queryset = WebFinding.objects.filter(created_at__lte=upper_bound)
        if cursor.created_at and cursor.finding_id:
            queryset = queryset.filter(
                Q(created_at__gt=cursor.created_at)
                | Q(
                    created_at=cursor.created_at,
                    pk__gt=cursor.finding_id,
                )
            )
        queryset = queryset.order_by('created_at', 'pk')
        last_created_at = cursor.created_at
        last_finding_id = cursor.finding_id
        pending_batch = []
        for finding in queryset.iterator(chunk_size=CHUNK_SIZE):
            pending_batch.append(
                StructureCurationBatchFinding(
                    finding_id=finding.pk,
                    status=StructureCurationBatchFinding.Status.PENDING,
                )
            )
            last_created_at = finding.created_at
            last_finding_id = finding.pk
            if len(pending_batch) >= CHUNK_SIZE:
                _insert_new_queue_findings(pending_batch)
                pending_batch = []
        if pending_batch:
            _insert_new_queue_findings(pending_batch)
        if last_created_at is not None:
            cursor.created_at = last_created_at
            cursor.finding_id = last_finding_id
            cursor.save(update_fields=['created_at', 'finding_id'])

        pending_ids = list(
            StructureCurationBatchFinding.objects.filter(
                batch__isnull=True,
                status=StructureCurationBatchFinding.Status.PENDING,
            ).values_list('finding_id', flat=True)
        )
        seed = random.SystemRandom().getrandbits(63)
        if full_scan:
            selected_ids = pending_ids
            batch_limit = len(selected_ids)
        else:
            selected_ids = random.Random(seed).sample(
                pending_ids,
                min(batch_limit, len(pending_ids)),
            )
        batch = StructureCurationBatch.objects.create(
            run_id=run_id,
            batch_limit=batch_limit,
            random_seed=seed,
            candidate_count=len(pending_ids),
            selected_count=len(selected_ids),
        )
        StructureCurationBatchFinding.objects.filter(
            finding_id__in=selected_ids,
            batch__isnull=True,
            status=StructureCurationBatchFinding.Status.PENDING,
        ).update(
            batch=batch,
            status=StructureCurationBatchFinding.Status.SELECTED,
        )

        state = read_run_state(run_id)
        state['batch_id'] = str(batch.pk)
        state['batch_limit'] = batch_limit
        state['batch_candidate_count'] = batch.candidate_count
        state['batch_selected_count'] = batch.selected_count
        state['batch_seed'] = batch.random_seed
        state['scan_mode'] = 'all_pending' if full_scan else 'random_batch'
        state['upper_bound'] = upper_bound.isoformat()
        state['start_after'] = None
        _write_state(run_id, state)
        return batch


def _insert_new_queue_findings(queue_items):
    finding_ids = [item.finding_id for item in queue_items]
    already_linked = set(
        StructureEvidence.objects.filter(
            web_finding_id__in=finding_ids,
        ).values_list('web_finding_id', flat=True)
    )
    for item in queue_items:
        if item.finding_id in already_linked:
            item.status = StructureCurationBatchFinding.Status.ACCEPTED
    StructureCurationBatchFinding.objects.bulk_create(
        queue_items,
        ignore_conflicts=True,
        batch_size=CHUNK_SIZE,
    )


def _sync_batch_finding_statuses(run_id):
    state = read_run_state(run_id)
    if not state.get('batch_id'):
        return
    records = {
        row['triage_original_inchikey']: row
        for row in iter_triaged_preview(run_id)
    }
    decisions_by_finding = defaultdict(list)
    for evidence in _iter_jsonl(get_evidence_report_path(run_id)):
        record = records.get(evidence['structure_inchikey'])
        if record:
            decisions_by_finding[evidence['web_finding_id']].append(
                record['triage_decision']
            )
    queue_items = list(
        StructureCurationBatchFinding.objects.filter(
            batch_id=state['batch_id'],
            status__in=[
                StructureCurationBatchFinding.Status.SELECTED,
                StructureCurationBatchFinding.Status.NEEDS_WORK,
            ],
        )
    )
    for item in queue_items:
        decisions = decisions_by_finding.get(str(item.finding_id), [])
        if not decisions:
            item.status = StructureCurationBatchFinding.Status.REJECTED
        elif any(
            decision in {'UNDECIDED', 'NEEDS_WORK'}
            for decision in decisions
        ):
            item.status = StructureCurationBatchFinding.Status.NEEDS_WORK
        elif any(decision == 'ACCEPTED' for decision in decisions):
            item.status = StructureCurationBatchFinding.Status.ACCEPTED
        else:
            item.status = StructureCurationBatchFinding.Status.REJECTED
    if queue_items:
        StructureCurationBatchFinding.objects.bulk_update(
            queue_items,
            ['status'],
            batch_size=CHUNK_SIZE,
        )
    batch = StructureCurationBatch.objects.get(pk=state['batch_id'])
    if batch.completed_at and batch.status != StructureCurationBatch.Status.COMMITTED:
        has_work = batch.findings.filter(
            status__in=[
                StructureCurationBatchFinding.Status.SELECTED,
                StructureCurationBatchFinding.Status.NEEDS_WORK,
            ]
        ).exists()
        batch.status = (
            StructureCurationBatch.Status.NEEDS_REVIEW
            if has_work
            else StructureCurationBatch.Status.READY
        )
        batch.save(update_fields=['status'])


def get_curation_batches():
    batches = []
    for batch in StructureCurationBatch.objects.all()[:50]:
        finding_counts = {
            row['status']: row['count']
            for row in batch.findings.values('status').annotate(
                count=Count('id'),
            )
        }
        batches.append({
            'run_id': str(batch.run_id),
            'created_at': batch.created_at,
            'completed_at': batch.completed_at,
            'status': batch.status,
            'superseded': batch.status == StructureCurationBatch.Status.SUPERSEDED,
            'batch_limit': batch.batch_limit,
            'candidate_count': batch.candidate_count,
            'selected_count': batch.selected_count,
            'needs_work_count': finding_counts.get('NEEDS_WORK', 0),
            'unresolved_count': (
                finding_counts.get('NEEDS_WORK', 0)
                + finding_counts.get('SELECTED', 0)
            ),
            'accepted_count': finding_counts.get('ACCEPTED', 0),
            'rejected_count': finding_counts.get('REJECTED', 0),
        })
    return batches


def reprocess_uncommitted_batch(previous_run_id, new_run_id):
    previous_run_id = str(uuid.UUID(str(previous_run_id)))
    new_run_id = str(uuid.UUID(str(new_run_id)))
    previous_state = read_run_state(previous_run_id)
    if (
        previous_state.get('status') != 'COMPLETED'
        or not previous_state.get('batch_id')
    ):
        raise ValueError('A completed curation batch is required.')
    if previous_state.get('database_commit'):
        raise ValueError(
            'This batch is fully committed and cannot be reprocessed.'
        )
    new_state = read_run_state(new_run_id)
    if new_state.get('status') != 'QUEUED' or new_state.get('batch_id'):
        raise ValueError('A new queued curation run is required.')

    with transaction.atomic():
        batch = StructureCurationBatch.objects.select_for_update().get(
            pk=previous_state['batch_id'],
        )
        if batch.status == StructureCurationBatch.Status.COMMITTED:
            raise ValueError(
                'This batch is fully committed and cannot be reprocessed.'
            )
        if batch.status == StructureCurationBatch.Status.SUPERSEDED:
            raise ValueError('This batch has already been reprocessed.')
        evidence_report_path = get_evidence_report_path(previous_run_id)
        if not evidence_report_path.is_file():
            raise ValueError(
                'The previous scan has no parsed structure-evidence report. '
                'Run a new curation scan instead of rebuilding this batch.'
            )
        parsed_finding_ids = {
            row['web_finding_id']
            for row in _iter_jsonl(evidence_report_path)
            if row.get('web_finding_id')
        }
        committed_finding_ids = StructureEvidence.objects.filter(
            web_finding_id__in=parsed_finding_ids,
        ).values('web_finding_id')
        unresolved_statuses = (
            StructureCurationBatchFinding.Status.SELECTED,
            StructureCurationBatchFinding.Status.NEEDS_WORK,
        )
        batch.findings.filter(
            finding_id__in=committed_finding_ids,
            status__in=unresolved_statuses,
        ).update(status=StructureCurationBatchFinding.Status.ACCEPTED)
        uncommitted_findings = batch.findings.filter(
            finding_id__in=parsed_finding_ids,
            status__in=unresolved_statuses,
        ).exclude(
            finding_id__in=committed_finding_ids,
        )
        finding_ids = list(
            uncommitted_findings.values_list('finding_id', flat=True)
        )
        new_batch = StructureCurationBatch.objects.create(
            run_id=new_run_id,
            batch_limit=len(finding_ids),
            random_seed=batch.random_seed,
            candidate_count=len(finding_ids),
            selected_count=len(finding_ids),
        )
        uncommitted_findings.update(
            batch=new_batch,
            status=StructureCurationBatchFinding.Status.SELECTED,
        )
        batch.status = StructureCurationBatch.Status.SUPERSEDED
        batch.save(update_fields=['status'])

    new_state.update({
        'batch_id': str(new_batch.pk),
        'batch_limit': len(finding_ids),
        'batch_candidate_count': len(finding_ids),
        'batch_selected_count': len(finding_ids),
        'batch_seed': batch.random_seed,
        'scan_mode': 'reprocessed_batch',
        'upper_bound': timezone.now().isoformat(),
        'start_after': None,
        'superseded_by_reprocessing': False,
    })
    new_state['updated_at'] = timezone.now().isoformat()
    _write_state(new_run_id, new_state)
    previous_state['superseded_by_reprocessing'] = True
    previous_state['updated_at'] = timezone.now().isoformat()
    _write_state(previous_run_id, previous_state)
    return len(finding_ids)


def _normalise_inchikey(value):
    if value is None:
        return None
    normalized = str(value).strip()
    if normalized.lower().startswith('inchikey='):
        normalized = normalized.split('=', 1)[1].strip()
    normalized = normalized.upper()
    return normalized or None


def _is_stereo_unspecified_variant(left, right):
    if not (
        INCHIKEY_PATTERN.fullmatch(left)
        and INCHIKEY_PATTERN.fullmatch(right)
    ):
        return False
    left_connectivity, left_stereo, left_version = left.split('-')
    right_connectivity, right_stereo, right_version = right.split('-')
    return (
        left_connectivity == right_connectivity
        and left_version == right_version
        and left_stereo != right_stereo
        and 'UHFFFAOYSA' in {left_stereo, right_stereo}
    )


def _structure_keys_compatible(left, right):
    return (
        left == right
        or _is_stereo_unspecified_variant(left, right)
    )


def _prefer_stereo_specific_keys(keys):
    keys = set(keys)
    return {
        key
        for key in keys
        if not (
            key.split('-')[1] == 'UHFFFAOYSA'
            and any(
                _is_stereo_unspecified_variant(key, other)
                for other in keys
                if other != key
            )
        )
    }


def _number(value):
    if value in (None, ''):
        return None
    try:
        number = float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_formula(value):
    if not isinstance(value, str):
        return None

    formula = html.unescape(value).strip()
    formula = re.sub(
        r'<\s*/?\s*(?:sub|sup)\s*>',
        '',
        formula,
        flags=re.IGNORECASE,
    )
    formula = re.sub(r'_\{(\d+)\}', r'\1', formula)
    formula = formula.translate(FORMULA_SUBSCRIPTS)
    if formula.lower().startswith('formula='):
        formula = formula.split('=', 1)[1].strip()
    formula = ''.join(formula.split()).replace('·', '.')
    formula = re.sub(r'[+-]+$', '', formula)
    if not formula or '+' in formula or '-' in formula:
        return None

    position = 0
    variable_multipliers = frozenset('abcdefghijklmnopqrstuvwxyz')

    def parse_integer():
        nonlocal position
        start = position
        while position < len(formula) and formula[position].isdigit():
            position += 1
        return int(formula[start:position]) if position > start else None

    def parse_multiplier():
        nonlocal position
        numerator = parse_integer()
        if numerator is None:
            return None
        if position < len(formula) and formula[position] == '/':
            position += 1
            denominator = parse_integer()
            if denominator is None or denominator <= 0:
                raise ValueError('Invalid fractional formula multiplier.')
            return Fraction(numerator, denominator)
        return Fraction(numerator)

    def parse_sequence(closing=None):
        nonlocal position
        element_counts = defaultdict(int)

        while position < len(formula):
            token = formula[position]
            if closing and token == closing:
                break
            if token in ')]':
                raise ValueError('Unbalanced formula group.')

            leading_count = parse_multiplier()
            if leading_count is None:
                leading_count = 1
            elif leading_count <= 0:
                raise ValueError('Invalid formula component.')

            if (
                position < len(formula)
                and formula[position] in variable_multipliers
                and position + 1 < len(formula)
                and formula[position + 1] in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ(['
            ):
                position += 1

            component_counts = defaultdict(int)
            while position < len(formula):
                token = formula[position]
                if token == '.' or (closing and token == closing):
                    break
                if token in ')]':
                    raise ValueError('Unbalanced formula group.')
                if token in '([':
                    expected = ')' if token == '(' else ']'
                    position += 1
                    nested_counts = parse_sequence(expected)
                    if position >= len(formula) or formula[position] != expected:
                        raise ValueError('Unclosed formula group.')
                    position += 1
                    multiplier = parse_multiplier()
                    if multiplier is None:
                        multiplier = 1
                    if (
                        position < len(formula)
                        and formula[position] in variable_multipliers
                    ):
                        position += 1
                    for element, count in nested_counts.items():
                        component_counts[element] += count * multiplier
                    continue
                if token.isupper():
                    match = FORMULA_ELEMENT_PATTERN.match(formula, position)
                    element = match.group(0)
                    if element not in FORMULA_ATOM_SYMBOLS:
                        raise ValueError('Unknown element symbol.')
                    position = match.end()
                    multiplier = parse_integer()
                    component_counts[element] += (
                        multiplier if multiplier is not None else 1
                    )
                    continue
                raise ValueError('Unsupported formula notation.')

            if not component_counts:
                raise ValueError('Invalid formula component.')
            for element, count in component_counts.items():
                element_counts[element] += count * leading_count

            if position < len(formula) and formula[position] == '.':
                position += 1
                if (
                    position >= len(formula)
                    or formula[position] == '.'
                    or (closing and formula[position] == closing)
                ):
                    raise ValueError('Invalid formula component.')
                continue
            break
        return element_counts

    try:
        element_counts = parse_sequence()
        if position != len(formula) or not element_counts:
            raise ValueError('Invalid formula.')
        denominator_scale = math.lcm(
            *(Fraction(count).denominator for count in element_counts.values())
        )
        element_counts = {
            element: int(count * denominator_scale)
            for element, count in element_counts.items()
        }
    except (ValueError, RecursionError):
        return None

    hill_order = []
    if 'C' in element_counts:
        hill_order.extend(('C', 'H') if 'H' in element_counts else ('C',))
        hill_order.extend(sorted(
            element for element in element_counts if element not in {'C', 'H'}
        ))
    else:
        hill_order.extend(sorted(element_counts))

    return ''.join(
        element + (str(element_counts[element]) if element_counts[element] != 1 else '')
        for element in hill_order
    )


def _formula_matches_reported_mass(formula, molecular_weight):
    normalized = normalize_formula(formula)
    if not normalized or molecular_weight is None:
        return False
    try:
        from openbabel import openbabel as ob

        formula_weight = 0.0
        position = 0
        for match in re.finditer(r'([A-Z][a-z]?)(\d*)', normalized):
            if match.start() != position:
                return False
            atomic_number = ob.GetAtomicNum(match.group(1))
            if not atomic_number:
                return False
            formula_weight += ob.GetMass(atomic_number) * int(
                match.group(2) or 1
            )
            position = match.end()
        if position != len(normalized):
            return False
    except ImportError as exc:
        raise RuntimeError(
            'Open Babel is required to validate fractional formula masses.'
        ) from exc
    return _chemistry_values_match(
        'molecular_weight',
        molecular_weight,
        formula_weight,
    )


def _has_fractional_formula_component(formula):
    if not isinstance(formula, str):
        return False
    plain_formula = html.unescape(formula)
    plain_formula = re.sub(r'<[^>]*>', '', plain_formula)
    plain_formula = plain_formula.replace('·', '.')
    return bool(re.search(r'(?:^|[.])\s*\d+\s*/\s*\d+', plain_formula))


def _derive_structure_values(record):
    try:
        from openbabel import openbabel as ob
        from openbabel import pybel
    except ImportError as exc:
        raise RuntimeError(
            'Open Babel is required to validate structure data.'
        ) from exc

    molecules = []
    failures = []
    for field, notation in (
        ('inchi', 'inchi'),
        ('canonical_smiles', 'smi'),
    ):
        value = record.get(field)
        if not value:
            continue
        try:
            molecule = pybel.readstring(notation, str(value))
            if molecule is None:
                raise ValueError('Open Babel could not parse the value.')
            source_inchikey = None
            if field == 'inchi' and str(value).startswith('InChI=1/'):
                source_inchikey = _normalise_inchikey(
                    molecule.write('inchikey').strip()
                )
                standard_inchi = 'InChI=1S/' + str(value)[len('InChI=1/'):]
                standard_molecule = pybel.readstring('inchi', standard_inchi)
                if (
                    standard_molecule is not None
                    and standard_molecule.write('inchi').strip()
                    == standard_inchi
                ):
                    molecule = standard_molecule
                else:
                    source_inchikey = None
            isomeric_smiles = molecule.write('can').strip().split()[0]
            non_isomeric_molecule = pybel.Molecule(ob.OBMol(molecule.OBMol))
            non_isomeric_molecule.OBMol.DeleteData(ob.StereoData)
            canonical_smiles = (
                non_isomeric_molecule.write('can').strip().split()[0]
            )
            inchi = molecule.write('inchi').strip()
            inchikey = _normalise_inchikey(
                molecule.write('inchikey').strip()
            )
            if not inchikey:
                raise ValueError('Open Babel could not calculate an InChIKey.')
            derived_structure = {
                'source': field,
                'inchikey': inchikey,
                'inchi': inchi,
                'canonical_smiles': canonical_smiles,
                'isomeric_smiles': isomeric_smiles,
                'formula': molecule.formula,
                'molecular_weight': float(molecule.molwt),
                'exact_mass': float(molecule.exactmass),
                'toolkit': 'Open Babel',
                'toolkit_version': ob.OBReleaseVersion(),
                'policy': 'canonical representations; no parent or salt normalization',
            }
            if source_inchikey and source_inchikey != inchikey:
                derived_structure['source_inchikey'] = source_inchikey
            molecules.append(derived_structure)
        except Exception as exc:
            failures.append({'source': field, 'error': str(exc)[:300]})
    return molecules, failures


def validate_candidate(record):
    reported_key = _normalise_inchikey(record.get('inchikey'))
    derived_values, structure_errors = _derive_structure_values(record)
    standardized_keys = {
        item['inchikey']
        for item in derived_values
        if item.get('source_inchikey') == reported_key
    }
    if reported_key and len(standardized_keys) == 1:
        reported_key = standardized_keys.pop()
    reported_key_valid = bool(
        reported_key and INCHIKEY_PATTERN.fullmatch(reported_key)
    )
    derived_keys = {
        item['inchikey'] for item in derived_values if item['inchikey']
    }
    sorted_derived_keys = sorted(derived_keys)
    has_structure_conflict = any(
        not _structure_keys_compatible(left, right)
        for index, left in enumerate(sorted_derived_keys)
        for right in sorted_derived_keys[index + 1:]
    )

    if not reported_key_valid and has_structure_conflict:
        return None, {
            'reported_key': reported_key,
            'reason': 'structure_key_conflict',
            'derived_keys': sorted(derived_keys),
            'derived_structures': derived_values,
            'structure_errors': structure_errors,
        }

    if reported_key_valid:
        key = reported_key
        key_origin = 'reported'
    elif derived_keys:
        key = sorted(_prefer_stereo_specific_keys(derived_keys))[0]
        key_origin = 'derived'
    else:
        return None, {
            'reported_key': reported_key,
            'reason': (
                'invalid_inchikey'
                if reported_key
                else 'no_inchikey_or_derivable_structure'
            ),
            'structure_errors': structure_errors,
        }

    key_checks = []
    if reported_key:
        key_checks.append(
            'reported_key_valid' if reported_key_valid else 'reported_key_invalid'
        )
    if has_structure_conflict:
        key_checks.append('inchi_smiles_disagree')
    elif len(derived_keys) > 1:
        key_checks.append('stereo_unspecified')

    if derived_keys:
        if reported_key_valid and has_structure_conflict:
            key_checks.append(
                'reported_key_matches_one_structure'
                if reported_key in derived_keys
                else 'reported_derived_key_mismatch'
            )
        elif reported_key_valid and reported_key in derived_keys:
            key_checks.append('structure_key_match')
        elif reported_key_valid and any(
            _is_stereo_unspecified_variant(reported_key, derived_key)
            for derived_key in derived_keys
        ):
            key_checks.append('reported_key_stereo_unspecified')
        elif reported_key_valid:
            key_checks.append('reported_derived_key_mismatch')
        else:
            key_checks.append('structure_key_derived')
    elif not structure_errors:
        key_checks.append('key_only')
    if structure_errors:
        key_checks.append('structure_parse_error')

    derived = derived_values[0] if derived_values else {}
    formula = record.get('molecular_formula')
    formula_check = None
    normalized_formula = normalize_formula(formula)
    if formula:
        formula_results = [
            (
                'unparseable'
                if normalized_formula is None
                or normalize_formula(value.get('formula')) is None
                else 'match'
                if normalized_formula == normalize_formula(value.get('formula'))
                else 'mismatch'
            )
            for value in derived_values
            if value.get('formula')
        ]
        formula_check = (
            'match'
            if 'match' in formula_results
            else 'mismatch'
            if 'mismatch' in formula_results
            else 'unparseable'
            if formula_results
            else None
        )

    molecular_weight = _number(record.get('molecular_weight'))
    molecular_weight_check = None
    if molecular_weight is not None:
        weight_results = [
            _chemistry_values_match(
                'molecular_weight',
                molecular_weight,
                value['molecular_weight'],
            )
            for value in derived_values
            if value.get('molecular_weight') is not None
        ]
        if weight_results:
            molecular_weight_check = (
                'match' if any(weight_results) else 'mismatch'
            )

    exact_mass = _number(record.get('exact_mass'))
    exact_mass_check = None
    if exact_mass is not None:
        mass_results = [
            _chemistry_values_match(
                'exact_mass',
                exact_mass,
                value['exact_mass'],
            )
            for value in derived_values
            if value.get('exact_mass') is not None
        ]
        if mass_results:
            exact_mass_check = 'match' if any(mass_results) else 'mismatch'

    reported_stoichiometry_mismatch = (
        _has_fractional_formula_component(formula)
        and formula_check == 'mismatch'
        and molecular_weight_check == 'mismatch'
        and _formula_matches_reported_mass(
            formula,
            molecular_weight,
        )
        and len(derived_values) > 1
        and all(
            _structure_keys_compatible(left, right)
            for index, left in enumerate(sorted_derived_keys)
            for right in sorted_derived_keys[index + 1:]
        )
    )

    derived_validations = []
    for derived_value in derived_values:
        computed_formula = normalize_formula(derived_value.get('formula'))
        if formula and derived_value.get('formula'):
            formula_result = (
                'unparseable'
                if normalized_formula is None or computed_formula is None
                else 'match'
                if normalized_formula == computed_formula
                else 'mismatch'
            )
        else:
            formula_result = None

        computed_weight = _number(derived_value.get('molecular_weight'))
        weight_result = (
            'match'
            if molecular_weight is not None
            and computed_weight is not None
            and _chemistry_values_match(
                'molecular_weight',
                molecular_weight,
                computed_weight,
            )
            else 'mismatch'
            if molecular_weight is not None and computed_weight is not None
            else None
        )
        computed_exact_mass = _number(derived_value.get('exact_mass'))
        exact_mass_result = (
            'match'
            if exact_mass is not None
            and computed_exact_mass is not None
            and _chemistry_values_match(
                'exact_mass',
                exact_mass,
                computed_exact_mass,
            )
            else 'mismatch'
            if exact_mass is not None and computed_exact_mass is not None
            else None
        )
        derived_validations.append({
            **derived_value,
            'reported_checks': {
                'formula': formula_result,
                'molecular_weight': weight_result,
                'exact_mass': exact_mass_result,
            },
        })

    candidate = dict(record)
    candidate.update({
        'inchikey': key,
        'reported_inchikey': reported_key,
        'inchikey_origin': key_origin,
        'normalized_formula': normalized_formula,
        'computed_structure_values': derived,
        'validation': {
            'key_checks': key_checks,
            'formula': formula_check,
            'molecular_weight': molecular_weight_check,
            'exact_mass': exact_mass_check,
            'reported_stoichiometry_mismatch': (
                reported_stoichiometry_mismatch
            ),
            'derived_structures': derived_validations,
            'structure_errors': structure_errors,
        },
    })
    return candidate, None


def _parser_for_provider(provider_code):
    parser_key = PROVIDER_ALIASES.get(provider_code, provider_code)
    config = PROVIDER_CONFIGS.get(parser_key)
    return config['parser'] if config else None


def _findings_queryset(state):
    if state.get('batch_id'):
        selected = StructureCurationBatchFinding.objects.filter(
            batch_id=state['batch_id'],
            status=StructureCurationBatchFinding.Status.SELECTED,
        ).values('finding_id')
        queryset = WebFinding.objects.filter(pk__in=selected)
        if state['cursor_updated_at'] and state['cursor_id']:
            queryset = queryset.filter(
                Q(updated_at__gt=state['cursor_updated_at'])
                | Q(
                    updated_at=state['cursor_updated_at'],
                    pk__gt=state['cursor_id'],
                )
            )
        return queryset.order_by('updated_at', 'pk')
    queryset = WebFinding.objects.filter(
        updated_at__lte=state['upper_bound'],
    )
    if state['cursor_updated_at'] and state['cursor_id']:
        queryset = queryset.filter(
            Q(updated_at__gt=state['cursor_updated_at'])
            | Q(
                updated_at=state['cursor_updated_at'],
                pk__gt=state['cursor_id'],
            )
        )
    elif state['start_after']:
        queryset = queryset.filter(updated_at__gte=state['start_after'])
    return queryset.order_by('updated_at', 'pk')


def count_findings(state):
    return _findings_queryset(state).count()


def process_scan_chunk(run_id):
    run_dir = _run_dir(run_id)
    state = read_run_state(run_id)
    if state['total_findings'] is None:
        state['status'] = 'RUNNING'
        state['total_findings'] = count_findings(state)

    rows = list(
        _findings_queryset(state)
        .select_related(
            'search_task__web_source',
            'search_task__search_term',
        )[:CHUNK_SIZE]
    )
    page_path = run_dir / 'pages' / f"{state['page_number']:08d}.jsonl"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_temp = page_path.with_suffix('.tmp')
    new_stats = dict(state['stats'])

    with page_temp.open('w', encoding='utf-8') as page_file:
        for finding in rows:
            source = finding.search_task.web_source if finding.search_task else None
            provider_code = (
                (source.provider_code or '').strip().lower() if source else ''
            )
            if not _parser_for_provider(provider_code):
                new_stats['unsupported_provider'] += 1
                continue
            try:
                drafts, rejected = build_finding_drafts(finding)
                if not drafts and not rejected:
                    new_stats['missing_key'] += 1
                for draft in drafts:
                    if draft['evidence']['assessment'] == 'CONFLICTS':
                        new_stats['ambiguous_structure'] += 1
                    if (
                        draft['evidence']['assessment'] == 'SUPPORTS'
                        and draft['evidence']['validation']['computed_inchikeys']
                    ):
                        new_stats['derived_keys'] += 1
                    page_file.write(json.dumps(draft, ensure_ascii=False) + '\n')
                for invalid in rejected:
                    if invalid['reason'] == 'invalid_inchikey':
                        new_stats['invalid_key'] += 1
                    else:
                        new_stats['missing_key'] += 1
                    page_file.write(json.dumps({
                        'rejected': invalid,
                    }, ensure_ascii=False) + '\n')
            except Exception as exc:
                new_stats['parser_errors'] += 1
                page_file.write(json.dumps({
                    'rejected': {
                        'finding_id': str(finding.pk),
                        'provider_code': provider_code,
                        'source_url': finding.source_url,
                        'reason': 'parser_error',
                        'error': str(exc)[:300],
                    },
                }) + '\n')

        page_file.flush()
        os.fsync(page_file.fileno())
    os.replace(page_temp, page_path)

    if rows:
        state['cursor_updated_at'] = rows[-1].updated_at.isoformat()
        state['cursor_id'] = str(rows[-1].pk)
    state['page_number'] += 1
    state['processed_findings'] += len(rows)
    state['stats'] = new_stats
    state['updated_at'] = timezone.now().isoformat()
    finished = len(rows) < CHUNK_SIZE
    state['status'] = 'FINALIZING' if finished else 'RUNNING'
    _write_state(run_id, state)
    return finished


def _partition_number(key):
    digest = hashlib.sha256(key.encode('utf-8')).digest()
    return int.from_bytes(digest[:2], 'big') % PARTITION_COUNT


STRUCTURE_DRAFT_FIELDS = (
    'inchi',
    'canonical_smiles',
    'isomeric_smiles',
    'molecular_formula',
    'molecular_weight',
    'exact_mass',
    'standardization_toolkit',
    'standardization_version',
    'standardization_policy',
)


def _new_structure_group(inchikey):
    return {
        'inchikey': inchikey,
        'field_variants': {field: {} for field in STRUCTURE_DRAFT_FIELDS},
        'evidence_by_finding': {},
    }


def _merge_structure_draft(group, draft):
    evidence = draft['evidence']
    finding_id = evidence['web_finding_id']
    current = group['evidence_by_finding'].get(finding_id)
    if current is None:
        current = {
            'structure_inchikey': group['inchikey'],
            'web_finding_id': finding_id,
            'assessment': evidence['assessment'],
            'provider_code': evidence['provider_code'],
            'source_url': evidence['source_url'],
            'finding_updated_at': evidence['finding_updated_at'],
            'target_representation': evidence['target_representation'],
            'reported_values': [evidence['reported_values']],
            'validation': [evidence['validation']],
        }
        group['evidence_by_finding'][finding_id] = current
    else:
        if evidence['assessment'] == 'CONFLICTS':
            current['assessment'] = 'CONFLICTS'
        elif (
            evidence['assessment'] == 'SUPPORTS'
            and current['assessment'] == 'INDETERMINATE'
        ):
            current['assessment'] = 'SUPPORTS'
        current['reported_values'].append(evidence['reported_values'])
        current['validation'].append(evidence['validation'])

    if evidence['assessment'] == 'SUPPORTS':
        computed_values = list(draft['computed_values'])
        reported_formula = normalize_formula(
            evidence['reported_values'].get('molecular_formula'),
        )
        matching_formula_values = [
            computed
            for computed in computed_values
            if reported_formula
            and normalize_formula(computed.get('formula')) == reported_formula
        ]
        if matching_formula_values:
            computed_values = matching_formula_values[:1]
        elif computed_values:
            computed_values = computed_values[:1]
        for computed in computed_values:
            if computed['inchikey'] != group['inchikey']:
                continue
            values = {
                'inchi': computed['inchi'],
                'canonical_smiles': computed['canonical_smiles'],
                'isomeric_smiles': computed['isomeric_smiles'],
                'molecular_formula': normalize_formula(computed['formula']),
                'molecular_weight': round(computed['molecular_weight'], 8),
                'exact_mass': round(computed['exact_mass'], 8),
                'standardization_toolkit': computed['toolkit'],
                'standardization_version': computed['toolkit_version'],
                'standardization_policy': computed['policy'],
            }
            for field, value in values.items():
                if value in (None, ''):
                    continue
                variants = group['field_variants'][field]
                variant = next(
                    (
                        item for item in variants.values()
                        if _chemistry_values_match(
                            field,
                            item['value'],
                            value,
                        )
                    ),
                    None,
                )
                if variant is None:
                    normalized = json.dumps(
                        value,
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    variant = variants.setdefault(
                        normalized,
                        {
                            'value': value,
                            'finding_ids': set(),
                            'sources': set(),
                        },
                    )
                variant['finding_ids'].add(finding_id)
                if computed.get('source'):
                    variant.setdefault('sources', set()).add(
                        computed['source'],
                    )


def _serialize_structure_group(group, existing_keys, existing_values=None):
    structure_row = {
        'inchikey': group['inchikey'],
        'parent_id': None,
    }
    computed_value_variants = {}
    review_flags = []
    for field, variants in group['field_variants'].items():
        serialized_variants = [
            {
                'value': variant['value'],
                'finding_count': len(variant['finding_ids']),
                'finding_ids': sorted(variant['finding_ids']),
                'sources': sorted(variant.get('sources', set())),
            }
            for variant in variants.values()
        ]
        computed_value_variants[field] = serialized_variants
        if len(serialized_variants) == 1:
            structure_row[field] = serialized_variants[0]['value']
        elif serialized_variants and field in {
            'canonical_smiles',
            'isomeric_smiles',
        }:
            structure_row[field] = sorted(
                serialized_variants,
                key=lambda variant: (
                    -variant['finding_count'],
                    variant['value'],
                ),
            )[0]['value']
        else:
            structure_row[field] = None
            if len(serialized_variants) > 1:
                review_flags.append(f'conflicting_computed_{field}')

    existing_row = (existing_values or {}).get(group['inchikey'])
    if existing_row:
        for field in STRUCTURE_DRAFT_FIELDS:
            if structure_row.get(field) in (None, ''):
                existing_value = existing_row.get(field)
                if (
                    field in {'molecular_weight', 'exact_mass'}
                    and existing_value is not None
                ):
                    existing_value = float(existing_value)
                structure_row[field] = existing_value
        for field in (
            'canonical_smiles',
            'isomeric_smiles',
            'molecular_formula',
            'molecular_weight',
            'exact_mass',
        ):
            existing_value = existing_row.get(field)
            if existing_value in (None, ''):
                continue
            incoming_values = [
                variant['value']
                for variant in group['field_variants'][field].values()
            ]
            if not incoming_values or any(
                _chemistry_values_match(field, incoming, existing_value)
                for incoming in incoming_values
            ):
                continue
            computed_value_variants.setdefault(field, []).append({
                'value': existing_value,
                'finding_count': 0,
                'finding_ids': [],
                'providers': ['existing Structure'],
                'source': 'existing Structure',
            })
            review_flags.append(f'conflicting_existing_{field}')

    evidence_rows = list(group['evidence_by_finding'].values())
    supporting_rows = [
        row for row in evidence_rows if row['assessment'] == 'SUPPORTS'
    ]
    key_only_evidence = bool(evidence_rows) and all(
        row['assessment'] == 'INDETERMINATE'
        and row['validation']
        and all(
            'key_only' in validation.get('key_checks', [])
            and not validation.get('computed_inchikeys')
            for validation in row['validation']
        )
        for row in evidence_rows
    )
    existing_has_structure = bool(
        existing_row
        and (
            existing_row.get('inchi')
            or existing_row.get('canonical_smiles')
            or existing_row.get('isomeric_smiles')
        )
    )
    if not supporting_rows and not key_only_evidence and not existing_has_structure:
        review_flags.append('no_supporting_structure_evidence')
    if any(row['assessment'] == 'CONFLICTS' for row in evidence_rows):
        review_flags.append('conflicting_source_evidence')
    for row in evidence_rows:
        for validation in row['validation']:
            key_checks = validation.get('key_checks', [])
            if 'inchi_smiles_disagree' in key_checks:
                review_flags.append('conflicting_structure_inputs')
            if 'reported_derived_key_mismatch' in key_checks:
                review_flags.append('reported_key_mismatch')
            stoichiometry_mismatch = validation.get(
                'reported_stoichiometry_mismatch',
                False,
            )
            if stoichiometry_mismatch:
                review_flags.append(
                    'reported_salt_stoichiometry_mismatch'
                )
            computed_rows = validation.get(
                'computed_values_for_candidate',
                [],
            )
            checked_fields = {
                field
                for computed in computed_rows
                for field in computed.get('reported_checks', {})
            }
            for field in checked_fields:
                results = [
                    computed.get('reported_checks', {}).get(field)
                    for computed in computed_rows
                ]
                if 'match' in results:
                    continue
                issue_results = {
                    result for result in results
                    if result in {'mismatch', 'unparseable'}
                }
                for result in issue_results:
                    if (
                        stoichiometry_mismatch
                        and field in {'formula', 'molecular_weight'}
                    ):
                        continue
                    review_flags.append(f'reported_{field}_{result}')
    review_flags = sorted(set(review_flags))
    evidence_by_finding = {
        row['web_finding_id']: row for row in evidence_rows
    }
    for variants in computed_value_variants.values():
        for variant in variants:
            if variant.get('source') != 'existing Structure':
                variant['providers'] = sorted({
                    evidence_by_finding[finding_id]['provider_code']
                    for finding_id in variant.get('finding_ids', [])
                    if finding_id in evidence_by_finding
                    and evidence_by_finding[finding_id]['provider_code']
                })
    structure_row.update({
        'evidence_count': len(evidence_rows),
        'supporting_finding_count': len(supporting_rows),
        'key_only_placeholder': (
            key_only_evidence
            and not structure_row.get('inchi')
            and not structure_row.get('canonical_smiles')
            and not structure_row.get('isomeric_smiles')
        ),
        'provider_count': len({
            row['provider_code'] for row in supporting_rows if row['provider_code']
        }),
        'review_status': (
            'REVIEW'
            if review_flags
            else 'SUPPORTED'
        ),
        'review_flags': review_flags,
        'computed_value_variants': computed_value_variants,
        'already_in_structure_database': (
            group['inchikey'] in existing_keys
        ),
    })
    return structure_row, evidence_rows


def _chemistry_values_match(field, left, right):
    if field == 'molecular_formula':
        left_formula = normalize_formula(left)
        right_formula = normalize_formula(right)
        return left_formula is not None and left_formula == right_formula
    if field == 'molecular_weight':
        left_value = float(left)
        right_value = float(right)
        scale = max(abs(left_value), abs(right_value))
        return (
            left_value == right_value
            if scale == 0
            else abs(left_value - right_value) / scale
            <= MASS_RELATIVE_TOLERANCE
        )
    if field == 'exact_mass':
        left_value = float(left)
        right_value = float(right)
        scale = max(abs(left_value), abs(right_value))
        return (
            left_value == right_value
            if scale == 0
            else abs(left_value - right_value) / scale
            <= MASS_RELATIVE_TOLERANCE
        )
    if field in {'canonical_smiles', 'isomeric_smiles'}:
        from openbabel import openbabel as ob
        from openbabel import pybel

        left_molecule = pybel.readstring('smi', str(left))
        right_molecule = pybel.readstring('smi', str(right))
        if left_molecule is None or right_molecule is None:
            return left == right
        if field == 'canonical_smiles':
            left_molecule = pybel.Molecule(ob.OBMol(left_molecule.OBMol))
            right_molecule = pybel.Molecule(ob.OBMol(right_molecule.OBMol))
            left_molecule.OBMol.DeleteData(ob.StereoData)
            right_molecule.OBMol.DeleteData(ob.StereoData)
        left_canonical = left_molecule.write('can').strip().split()[0]
        right_canonical = right_molecule.write('can').strip().split()[0]
        return left_canonical == right_canonical
    return left == right


EDITABLE_CHEMISTRY_FIELDS = {
    'molecular_formula',
    'molecular_weight',
    'exact_mass',
}


def _reported_issue_field(flag):
    issue = flag.removeprefix('reported_')
    if issue.startswith('formula'):
        return 'molecular_formula'
    if issue.startswith('molecular_weight') or issue.startswith('weight'):
        return 'molecular_weight'
    if issue.startswith('exact_mass'):
        return 'exact_mass'
    if 'structure' in issue or 'key' in issue:
        return 'corrected_smiles'
    return None


def _existing_issue_field(flag):
    field = flag.removeprefix('conflicting_existing_')
    return field if field in {
        *EDITABLE_CHEMISTRY_FIELDS,
        'canonical_smiles',
        'isomeric_smiles',
    } else None


def _issue_edit_fields(flags):
    fields = set()
    for flag in flags:
        if flag.startswith('conflicting_existing_'):
            field = _existing_issue_field(flag)
            if field in EDITABLE_CHEMISTRY_FIELDS:
                fields.add(field)
        if flag == 'conflicting_structure_inputs' or flag == 'reported_key_mismatch':
            fields.add('corrected_smiles')
        elif flag.startswith('conflicting_computed_'):
            field = flag.removeprefix('conflicting_computed_')
            if field in EDITABLE_CHEMISTRY_FIELDS:
                fields.add(field)
        elif flag.startswith('reported_'):
            field = _reported_issue_field(flag)
            if field:
                fields.add(field)
        elif flag == 'conflicting_source_evidence':
            fields.add('corrected_smiles')
        elif flag == 'parent_generation_failed':
            fields.add('corrected_smiles')
    return sorted(fields)


def _matching_reported_values(value, field):
    matching = []
    field_tokens = {
        'molecular_formula': ('formula',),
        'molecular_weight': ('molecularweight', 'molecular_weight', 'weight'),
        'exact_mass': ('exactmass', 'exact_mass'),
        'corrected_smiles': ('smile', 'inchi', 'inchikey'),
    }[field]

    def visit(current, path=''):
        if isinstance(current, dict):
            for key, child in current.items():
                key_text = str(key).lower().replace(' ', '')
                child_path = f'{path}.{key}' if path else str(key)
                if any(token in key_text for token in field_tokens):
                    matching.append({
                        'field': child_path,
                        'value': child,
                    })
                elif isinstance(child, (dict, list)):
                    visit(child, child_path)
        elif isinstance(current, list):
            for index, child in enumerate(current):
                visit(child, f'{path}[{index}]')

    visit(value)
    return matching


def _evidence_issue_values(evidence, field):
    reported = evidence.get('reported_values', [])
    if isinstance(reported, dict):
        reported = [reported]
    reported_values = [
        item
        for value in reported
        for item in _matching_reported_values(value, field)
    ]
    for item in reported_values:
        if 'inchikey' in item['field'].lower():
            normalized = _normalise_inchikey(item['value'])
        elif field == 'molecular_formula':
            normalized = normalize_formula(item['value'])
        else:
            continue
        if normalized and normalized != item['value']:
            item['raw_value'] = item['value']
            item['value'] = normalized
            item['normalized'] = True

    computed_values = []
    validations = evidence.get('validation', [])
    if isinstance(validations, dict):
        validations = [validations]
    if not validations:
        validations = []
    for validation in validations:
        if not isinstance(validation, dict):
            continue
        if field == 'corrected_smiles':
            computed_structures = validation.get('derived_structures', [])
        else:
            computed_structures = validation.get(
                'computed_values_for_candidate',
                [],
            )
        computed_field = (
            'formula' if field == 'molecular_formula' else field
        )
        for computed in computed_structures:
            if not isinstance(computed, dict):
                continue
            value_fields = (
                (
                    'inchi',
                    'canonical_smiles',
                    'isomeric_smiles',
                    'inchikey',
                )
                if field == 'corrected_smiles'
                else (field,)
            )
            for value_field in value_fields:
                value = computed.get(
                    computed_field if field != 'corrected_smiles' else value_field
                )
                if value is not None:
                    computed_values.append({
                        'field': value_field,
                        'value': value,
                        'source': computed.get('source'),
                    })
    return {
        'reported_values': reported_values,
        'computed_values': computed_values,
    }


def _build_issue_details(record, evidence_rows):
    flags = record.get('original_review_flags', record.get('review_flags', []))
    details = []
    for flag in flags:
        if flag.startswith('conflicting_computed_'):
            field = flag.removeprefix('conflicting_computed_')
            variants = record.get('computed_value_variants', {}).get(field, [])
            details.append({
                'flag': flag,
                'label': _review_flag_description(flag),
                'field': field,
                'variants': variants,
                'evidence': [
                    {
                        'provider_code': provider,
                        'finding_ids': variant.get('finding_ids', []),
                        'value': variant['value'],
                    }
                    for variant in variants
                    for provider in variant.get('providers', [])
                ],
            })
            continue

        if flag.startswith('conflicting_existing_'):
            field = _existing_issue_field(flag)
            details.append({
                'flag': flag,
                'label': _review_flag_description(flag),
                'field': field or 'corrected_smiles',
                'variants': record.get(
                    'computed_value_variants',
                    {},
                ).get(field, []),
                'evidence': [],
            })
            continue

        if flag == 'reported_salt_stoichiometry_mismatch':
            stoichiometry_evidence = []
            for evidence in evidence_rows:
                validations = evidence.get('validation', [])
                if isinstance(validations, dict):
                    validations = [validations]
                if not any(
                    isinstance(validation, dict)
                    and validation.get(
                        'reported_stoichiometry_mismatch',
                        False,
                    )
                    for validation in validations
                ):
                    continue
                formula_values = _evidence_issue_values(
                    evidence,
                    'molecular_formula',
                )
                weight_values = _evidence_issue_values(
                    evidence,
                    'molecular_weight',
                )
                structure_values = _evidence_issue_values(
                    evidence,
                    'corrected_smiles',
                )
                stoichiometry_evidence.append({
                    'provider_code': evidence['provider_code'],
                    'assessment': evidence['assessment'],
                    'web_finding_id': evidence['web_finding_id'],
                    'reported_values': (
                        formula_values['reported_values']
                        + weight_values['reported_values']
                    ),
                    'computed_values': (
                        formula_values['computed_values']
                        + weight_values['computed_values']
                        + structure_values['computed_values']
                    ),
                    'structure_errors': [],
                })
            details.append({
                'flag': flag,
                'label': _review_flag_description(flag),
                'field': 'molecular_formula',
                'variants': [],
                'evidence': stoichiometry_evidence,
            })
            continue

        if flag.startswith('reported_'):
            field = _reported_issue_field(flag) or 'corrected_smiles'
        elif flag == 'conflicting_structure_inputs':
            field = 'corrected_smiles'
        else:
            field = 'corrected_smiles'

        details.append({
            'flag': flag,
            'label': _review_flag_description(flag),
            'field': field,
            'variants': [],
            'evidence': [
                issue_evidence
                for evidence in evidence_rows
                if (
                    evidence['assessment'] == 'CONFLICTS'
                    or _is_review_relevant_evidence(evidence)
                    or flag == 'no_supporting_structure_evidence'
                )
                if (
                    issue_evidence := {
                    'provider_code': evidence['provider_code'],
                    'assessment': evidence['assessment'],
                    'web_finding_id': evidence['web_finding_id'],
                    **_evidence_issue_values(evidence, field),
                    'structure_errors': [
                        error
                        for validation in (
                            evidence['validation']
                            if isinstance(evidence['validation'], list)
                            else [evidence['validation']]
                        )
                        if isinstance(validation, dict)
                        for error in validation.get('structure_errors', [])
                    ],
                    }
                )
                and (
                    issue_evidence['reported_values']
                    or issue_evidence['computed_values']
                    or issue_evidence['structure_errors']
                )
            ],
        })
    return details


def _run_rdkit_parent_worker(source_path, output_path, parent_path):
    try:
        subprocess.run(
            [
                sys.executable,
                '-m',
                'apps.registry.services.rdkit_parent_worker',
                str(source_path),
                str(output_path),
                str(parent_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(settings.BASE_DIR),
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()[-1000:]
        raise RuntimeError(
            f'RDKit parent generation failed: {detail}'
        ) from exc


def _mark_parent_database_presence(parent_path):
    parent_keys = sorted(
        record['inchikey'] for record in _iter_jsonl(parent_path)
    )
    existing_keys = set()
    for start in range(0, len(parent_keys), 500):
        existing_keys.update(
            Structure.objects.filter(
                inchikey__in=parent_keys[start:start + 500]
            ).values_list('inchikey', flat=True)
        )
    temporary_path = parent_path.with_suffix('.checked.tmp')
    with temporary_path.open('w', encoding='utf-8') as output_file:
        for parent in _iter_jsonl(parent_path):
            parent['already_in_structure_database'] = (
                parent['inchikey'] in existing_keys
            )
            output_file.write(json.dumps(parent, ensure_ascii=False) + '\n')
        output_file.flush()
        os.fsync(output_file.fileno())
    os.replace(temporary_path, parent_path)


def _run_rdkit_for_row(row):
    with tempfile.TemporaryDirectory() as temporary_directory:
        source_path = Path(temporary_directory) / 'source.jsonl'
        output_path = Path(temporary_directory) / 'output.jsonl'
        parent_path = Path(temporary_directory) / 'parents.jsonl'
        source_path.write_text(
            json.dumps(row, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
        _run_rdkit_parent_worker(source_path, output_path, parent_path)
        result = next(_iter_jsonl(output_path))
        if result.get('worker_error'):
            raise ValueError(result['worker_error'])
        return result


def _review_complexity(flags):
    complex_flags = {
        'conflicting_source_evidence',
        'conflicting_structure_inputs',
        'reported_key_mismatch',
        'reported_derived_key_mismatch',
        'parent_generation_failed',
    }
    return 'COMPLEX' if complex_flags.intersection(flags) else 'SIMPLE'


def _review_flag_description(flag):
    if flag.startswith('conflicting_existing_'):
        field = flag.removeprefix('conflicting_existing_').replace('_', ' ')
        return (
            f'New findings disagree with the existing Structure {field}; '
            'the saved database value is shown for comparison'
        )
    labels = {
        'conflicting_source_evidence': (
            'Providers report incompatible structures for the same key'
        ),
        'conflicting_structure_inputs': (
            "A provider's InChI and SMILES produce different InChIKeys when parsed"
        ),
        'reported_key_mismatch': (
            'The reported InChIKey does not match the reported structure'
        ),
        'reported_salt_stoichiometry_mismatch': (
            'The provider formula and molecular weight agree with each other '
            'but describe a different fractional salt stoichiometry than the '
            'parsed structure'
        ),
        'no_supporting_structure_evidence': (
            'No provider evidence independently supports this structure'
        ),
        'parent_generation_failed': (
            'RDKit could not generate a parent structure'
        ),
    }
    if flag.startswith('conflicting_computed_'):
        field = flag.removeprefix('conflicting_computed_').replace('_', ' ')
        return f'Providers produced different computed {field} values'
    if flag.startswith('reported_'):
        details = flag.removeprefix('reported_').replace('_', ' ')
        return f'Reported {details} does not match the computed structure'
    return labels.get(flag, flag.replace('_', ' ').capitalize())


def _review_group_id(flags):
    return hashlib.sha256(
        json.dumps(sorted(flags), separators=(',', ':')).encode('utf-8')
    ).hexdigest()[:16]


def _iter_jsonl(path):
    if not path.exists():
        return
    with path.open(encoding='utf-8') as source_file:
        for line in source_file:
            if line.strip():
                yield json.loads(line)


def finalize_scan(run_id):
    run_dir = _run_dir(run_id)
    state = read_run_state(run_id)
    partitions_dir = run_dir / 'partitions'
    if partitions_dir.exists():
        for path in partitions_dir.iterdir():
            if path.is_file():
                path.unlink()
        partitions_dir.rmdir()
    partitions_dir.mkdir()

    invalid_temp = run_dir / 'invalid.jsonl.tmp'
    rejected_findings = 0
    rejection_reasons = defaultdict(int)
    with invalid_temp.open('w', encoding='utf-8') as invalid_file, ExitStack() as stack:
        partition_files = {}
        for page_path in sorted((run_dir / 'pages').glob('*.jsonl')):
            for entry in _iter_jsonl(page_path):
                if 'rejected' in entry:
                    rejected_findings += 1
                    rejection_reasons[entry['rejected']['reason']] += 1
                    invalid_file.write(
                        json.dumps(entry['rejected'], ensure_ascii=False) + '\n'
                    )
                elif entry.get('structure', {}).get('inchikey'):
                    partition = _partition_number(
                        entry['structure']['inchikey']
                    )
                    if partition not in partition_files:
                        partition_files[partition] = stack.enter_context(
                            (partitions_dir / f'{partition:03d}.jsonl').open(
                                'a', encoding='utf-8'
                            )
                        )
                    partition_files[partition].write(
                        json.dumps(entry, ensure_ascii=False) + '\n'
                    )

    os.replace(invalid_temp, run_dir / 'invalid.jsonl')
    structure_report_temp = run_dir / 'preview.jsonl.tmp'
    evidence_report_temp = run_dir / 'structure_evidence.jsonl.tmp'
    parent_report_temp = run_dir / 'parent_structures.jsonl.tmp'
    unique_count = 0
    evidence_count = 0
    evidence_assessments = defaultdict(int)
    with (
        structure_report_temp.open('w', encoding='utf-8') as structure_file,
        evidence_report_temp.open('w', encoding='utf-8') as evidence_file,
    ):
        for partition_path in sorted(partitions_dir.glob('*.jsonl')):
            groups = {}
            for draft in _iter_jsonl(partition_path):
                key = draft['structure']['inchikey']
                group = groups.setdefault(key, _new_structure_group(key))
                _merge_structure_draft(group, draft)

            keys = list(groups)
            existing_keys = set()
            existing_values = {}
            for start in range(0, len(keys), 500):
                existing_rows = Structure.objects.filter(
                    inchikey__in=keys[start:start + 500]
                ).values(
                    'inchikey',
                    'inchi',
                    'canonical_smiles',
                    'isomeric_smiles',
                    'molecular_formula',
                    'molecular_weight',
                    'exact_mass',
                    'standardization_toolkit',
                    'standardization_version',
                    'standardization_policy',
                )
                for existing_row in existing_rows:
                    existing_keys.add(existing_row['inchikey'])
                    existing_values[existing_row['inchikey']] = existing_row

            for key in sorted(keys):
                structure_row, evidence_rows = _serialize_structure_group(
                    groups[key],
                    existing_keys,
                    existing_values,
                )
                unique_count += 1
                structure_file.write(
                    json.dumps(structure_row, ensure_ascii=False) + '\n'
                )
                for evidence in sorted(
                    evidence_rows,
                    key=lambda row: row['web_finding_id'],
                ):
                    evidence_file.write(
                        json.dumps(evidence, ensure_ascii=False) + '\n'
                    )
                    evidence_count += 1
                    evidence_assessments[evidence['assessment']] += 1
        structure_file.flush()
        os.fsync(structure_file.fileno())
        evidence_file.flush()
        os.fsync(evidence_file.fileno())
    enriched_structure_temp = run_dir / 'preview.enriched.jsonl.tmp'
    _run_rdkit_parent_worker(
        structure_report_temp,
        enriched_structure_temp,
        parent_report_temp,
    )
    os.replace(enriched_structure_temp, structure_report_temp)
    _mark_parent_database_presence(parent_report_temp)

    review_count = 0
    review_flag_counts = defaultdict(int)
    review_group_members = defaultdict(list)
    review_group_flags = {}
    self_parent_count = 0
    already_existing_count = 0
    newly_supported_count = 0
    for structure_row in _iter_jsonl(structure_report_temp):
        if structure_row['review_status'] == 'REVIEW':
            review_count += 1
        if structure_row.get('already_in_structure_database'):
            already_existing_count += 1
        elif structure_row['review_status'] == 'SUPPORTED':
            newly_supported_count += 1
        self_parent_count += structure_row.get('parent_is_self', False)
        for flag in structure_row.get('review_flags', []):
            review_flag_counts[flag] += 1
        flags = structure_row.get('review_flags', [])
        if flags:
            group_id = _review_group_id(flags)
            review_group_members[group_id].append(structure_row['inchikey'])
            review_group_flags[group_id] = flags
    parent_count = sum(1 for _ in _iter_jsonl(parent_report_temp))
    os.replace(structure_report_temp, run_dir / 'preview.jsonl')
    os.replace(
        evidence_report_temp,
        run_dir / 'structure_evidence.jsonl',
    )
    os.replace(
        parent_report_temp,
        run_dir / 'parent_structures.jsonl',
    )

    state['status'] = 'COMPLETED'
    state['report_format_version'] = REPORT_FORMAT_VERSION
    state['unique_keys'] = unique_count
    state['evidence_rows'] = evidence_count
    state['conflicting_keys'] = review_count
    state['structures_supported'] = unique_count - review_count
    state['structures_needing_review'] = review_count
    state['already_in_structure_database_count'] = already_existing_count
    state['new_supported_structures'] = newly_supported_count
    state['parent_structures'] = parent_count
    state['self_parent_structures'] = self_parent_count
    state['review_groups'] = []
    for group_id, keys in sorted(
        review_group_members.items(),
        key=lambda item: (-len(item[1]), item[0]),
    ):
        flags = review_group_flags[group_id]
        state['review_groups'].append({
            'id': group_id,
            'flags': flags,
            'complexity': _review_complexity(flags),
            'count': len(keys),
            'sample_inchikeys': keys[:5],
        })
    state['review_flag_counts'] = [
        {'flag': flag, 'count': count}
        for flag, count in sorted(
            review_flag_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    state['evidence_assessments'] = dict(evidence_assessments)
    state['rejected_findings'] = rejected_findings
    state['rejection_reasons'] = [
        {'reason': reason, 'count': count}
        for reason, count in sorted(
            rejection_reasons.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    _auto_triage_fractional_stoichiometry_mismatches(run_id)
    _auto_triage_reported_inchi_counterion_pairs(run_id)
    _auto_accept_clear_salt_pairs(run_id)
    _auto_triage_reported_inchi_composition_pairs(run_id)
    _sync_batch_finding_statuses(run_id)
    if state.get('batch_id'):
        batch = StructureCurationBatch.objects.get(pk=state['batch_id'])
        batch.completed_at = timezone.now()
        has_work = batch.findings.filter(
            status__in=[
                StructureCurationBatchFinding.Status.SELECTED,
                StructureCurationBatchFinding.Status.NEEDS_WORK,
            ]
        ).exists()
        batch.status = (
            StructureCurationBatch.Status.NEEDS_REVIEW
            if has_work
            else StructureCurationBatch.Status.READY
        )
        batch.save(update_fields=['completed_at', 'status'])
        state['batch_status'] = batch.status
    state['finished_at'] = timezone.now().isoformat()
    state['updated_at'] = state['finished_at']
    _write_state(run_id, state)
    _atomic_json(
        _root() / 'checkpoint.json',
        {'updated_at': state['upper_bound']},
    )
    _clear_active_run(run_id)
    return state


def mark_run_failed(run_id, error):
    try:
        state = read_run_state(run_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return
    failed_from_status = state.get('status')
    state['status'] = 'FAILED'
    state['failed_from_status'] = failed_from_status
    state['error'] = str(error)[:1000]
    state['updated_at'] = timezone.now().isoformat()
    _write_state(run_id, state)
    if state.get('batch_id') and failed_from_status != 'FINALIZING':
        StructureCurationBatchFinding.objects.filter(
            batch_id=state['batch_id'],
            status=StructureCurationBatchFinding.Status.SELECTED,
        ).update(
            batch=None,
            status=StructureCurationBatchFinding.Status.PENDING,
        )
        StructureCurationBatch.objects.filter(
            pk=state['batch_id'],
        ).update(
            status=StructureCurationBatch.Status.FAILED,
        )
    _clear_active_run(run_id)


def can_retry_failed_finalization(run_id, state):
    if (
        state.get('status') != 'FAILED'
        or state.get('expected_report_format_version')
        != REPORT_FORMAT_VERSION
        or state.get('total_findings') is None
        or state.get('processed_findings') != state.get('total_findings')
    ):
        return False
    page_number = state.get('page_number')
    if not isinstance(page_number, int) or page_number < 1:
        return False
    run_dir = _run_dir(run_id)
    return all(
        (run_dir / 'pages' / f'{index:08d}.jsonl').is_file()
        for index in range(page_number)
    )


def retry_failed_finalization(run_id):
    run_id = str(uuid.UUID(str(run_id)))
    with _root_lock() as root:
        active_path = root / 'active_run.json'
        if active_path.exists():
            active = json.loads(active_path.read_text(encoding='utf-8'))
            active_state = read_run_state(active['id'])
            if active_state.get('status') in {
                'QUEUED',
                'RUNNING',
                'FINALIZING',
            }:
                raise RuntimeError(
                    f"Structure scan {active['id']} is already active."
                )

        state = read_run_state(run_id)
        if state.get('status') != 'FAILED':
            raise ValueError(
                'Only a failed curation scan can be finalized again.'
            )
        if not can_retry_failed_finalization(run_id, state):
            raise ValueError(
                'This scan did not finish processing its selected findings '
                'or its saved pages are unavailable; start a new scan.'
            )

        if state.get('batch_id'):
            with transaction.atomic():
                batch = StructureCurationBatch.objects.select_for_update().get(
                    pk=state['batch_id'],
                )
                if batch.status in {
                    StructureCurationBatch.Status.COMMITTED,
                    StructureCurationBatch.Status.SUPERSEDED,
                }:
                    raise ValueError(
                        'This curation batch can no longer be finalized.'
                    )
                if not batch.findings.exists():
                    if batch.selected_count != batch.candidate_count:
                        raise ValueError(
                            'The original batch membership is unavailable. '
                            'Start a new scan rather than guessing which '
                            'findings belonged to this batch.'
                        )
                    pending = StructureCurationBatchFinding.objects.filter(
                        batch__isnull=True,
                        status=StructureCurationBatchFinding.Status.PENDING,
                        finding__created_at__lte=state['upper_bound'],
                    )
                    if pending.count() != batch.selected_count:
                        raise ValueError(
                            'The original batch membership cannot be restored '
                            'safely. Start a new scan instead.'
                        )
                    pending.update(
                        batch=batch,
                        status=StructureCurationBatchFinding.Status.SELECTED,
                    )
                else:
                    batch.findings.update(
                        status=StructureCurationBatchFinding.Status.SELECTED,
                    )
                batch.status = StructureCurationBatch.Status.SCANNING
                batch.completed_at = None
                batch.save(update_fields=['status', 'completed_at'])

        state['status'] = 'FINALIZING'
        state['error'] = None
        state['failed_from_status'] = 'FINALIZING'
        state['updated_at'] = timezone.now().isoformat()
        _write_state(run_id, state)
        _atomic_json(active_path, {'id': run_id})
        return state


def _clear_active_run(run_id):
    with _root_lock() as root:
        active_path = root / 'active_run.json'
        if active_path.exists():
            try:
                active = json.loads(active_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                return
            if active.get('id') == str(uuid.UUID(str(run_id))):
                active_path.unlink(missing_ok=True)


def get_latest_run_id():
    path = _root() / 'latest_run.json'
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return str(uuid.UUID(value['id']))
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


TRIAGE_DECISIONS = {'ACCEPTED', 'NEEDS_WORK', 'REJECTED'}


def _triage_path(run_id):
    return _run_dir(run_id) / 'triage.json'


def get_triage_state(run_id):
    path = _triage_path(run_id)
    if not path.exists():
        return {'groups': {}, 'structures': {}}
    with path.open(encoding='utf-8') as triage_file:
        state = json.load(triage_file)
    state.setdefault('groups', {})
    state.setdefault('structures', {})
    return state


def get_review_groups(run_id, state=None):
    state = state or read_run_state(run_id)
    triage = get_triage_state(run_id)
    groups = []
    for group in state.get('review_groups', []):
        group = dict(group)
        group['decision'] = triage['groups'].get(
            group['id'],
            'UNDECIDED',
        )
        groups.append(group)
    return groups


def _is_review_relevant_evidence(evidence):
    if evidence.get('assessment') == 'CONFLICTS':
        return True
    validations = evidence.get('validation') or []
    if isinstance(validations, dict):
        validations = [validations]
    for validation in validations:
        if not isinstance(validation, dict):
            continue
        key_checks = validation.get('key_checks', [])
        if {
            'inchi_smiles_disagree',
            'reported_derived_key_mismatch',
        }.intersection(key_checks):
            return True
        for computed in validation.get('computed_values_for_candidate', []):
            if any(
                result in {'mismatch', 'unparseable'}
                for result in computed.get('reported_checks', {}).values()
            ):
                return True
    return False


@lru_cache(maxsize=8192)
def _salt_component_signatures(smiles_values):
    with tempfile.TemporaryDirectory() as temporary_directory:
        source_path = Path(temporary_directory) / 'source.jsonl'
        output_path = Path(temporary_directory) / 'signatures.jsonl'
        source_path.write_text(
            ''.join(
                json.dumps({'smiles': smiles}, ensure_ascii=False) + '\n'
                for smiles in smiles_values
            ),
            encoding='utf-8',
        )
        try:
            subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'apps.registry.services.rdkit_parent_worker',
                    '--salt-signatures',
                    str(source_path),
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=str(settings.BASE_DIR),
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()[-1000:]
            raise RuntimeError(
                f'RDKit salt-component analysis failed: {detail}'
            ) from exc
        return tuple(
            tuple(signature) if signature else None
            for record in _iter_jsonl(output_path)
            for signature in [record.get('signature')]
        )


def _is_salt_component_count_mismatch(inchi_structure, smiles_structure):
    inchi_smiles = (
        inchi_structure.get('isomeric_smiles')
        or inchi_structure.get('canonical_smiles')
    )
    smiles_smiles = (
        smiles_structure.get('isomeric_smiles')
        or smiles_structure.get('canonical_smiles')
    )
    inchi_signature, smiles_signature = _salt_component_signatures((
        inchi_smiles,
        smiles_smiles,
    ))
    if not inchi_signature or not smiles_signature:
        return False
    return (
        inchi_signature[0] == smiles_signature[0]
        and inchi_signature[1] == smiles_signature[1]
        and inchi_signature[2] != smiles_signature[2]
    )


def _formula_element_counts(formula):
    normalized = normalize_formula(formula)
    if not normalized:
        return None
    counts = {}
    position = 0
    for match in re.finditer(r'([A-Z][a-z]?)(\d*)', normalized):
        element = match.group(1)
        if (
            match.start() != position
            or element not in FORMULA_ELEMENTS
        ):
            return None
        counts[element] = int(match.group(2) or 1)
        position = match.end()
    return counts if position == len(normalized) else None


def _inchi_formula_has_only_extra_counterions(inchi_formula, smiles_formula):
    inchi_counts = _formula_element_counts(inchi_formula)
    smiles_counts = _formula_element_counts(smiles_formula)
    if not inchi_counts or not smiles_counts:
        return False
    elements = inchi_counts.keys() | smiles_counts.keys()
    differences = {
        element: inchi_counts.get(element, 0) - smiles_counts.get(element, 0)
        for element in elements
    }
    return (
        any(
            differences.get(element, 0) > 0
            for element in COUNTERION_ELEMENTS
        )
        and all(
            difference >= 0
            for element, difference in differences.items()
        )
        and all(
            not difference or element in COUNTERION_ELEMENTS
            for element, difference in differences.items()
        )
    )


def _reported_composition_check(evidence, source):
    matches = []
    validations = evidence.get('validation') or []
    if isinstance(validations, dict):
        validations = [validations]
    for validation in validations:
        if not isinstance(validation, dict):
            continue
        for computed in validation.get(
            'computed_values_for_candidate',
            [],
        ):
            if (
                isinstance(computed, dict)
                and computed.get('source') == source
                and isinstance(computed.get('reported_checks'), dict)
            ):
                matches.append(computed)
    if len(matches) != 1:
        return None
    return matches[0]


def _reported_inchi_preferred_pair(
    finding_evidence,
    structure_rows,
    evidence_by_structure,
    finding_id,
    require_counterion_difference=False,
):
    validations = []
    for evidence in finding_evidence:
        values = evidence.get('validation') or []
        validations.extend(values if isinstance(values, list) else [values])
    validation = next((
        item for item in validations
        if isinstance(item, dict)
        and 'inchi_smiles_disagree' in item.get('key_checks', [])
    ), None)
    if not validation:
        return None

    derived_by_source = defaultdict(dict)
    for derived in validation.get('derived_structures', []):
        if not isinstance(derived, dict):
            continue
        source = derived.get('source')
        key = _normalise_inchikey(derived.get('inchikey'))
        if source in {'inchi', 'canonical_smiles'} and key:
            derived_by_source[source][key] = derived
    if (
        len(derived_by_source['inchi']) != 1
        or len(derived_by_source['canonical_smiles']) != 1
    ):
        return None

    inchi_key, inchi_structure = next(
        iter(derived_by_source['inchi'].items())
    )
    smiles_key, smiles_structure = next(
        iter(derived_by_source['canonical_smiles'].items())
    )
    if (
        inchi_key == smiles_key
        or _normalise_inchikey(validation.get('reported_inchikey'))
        != inchi_key
        or not validation.get('reported_inchikey_valid')
        or (
            require_counterion_difference
            and not _inchi_formula_has_only_extra_counterions(
                inchi_structure.get('formula'),
                smiles_structure.get('formula'),
            )
        )
    ):
        return None

    inchi_evidence = next((
        row for row in finding_evidence
        if row.get('structure_inchikey') == inchi_key
    ), None)
    smiles_evidence = next((
        row for row in finding_evidence
        if row.get('structure_inchikey') == smiles_key
    ), None)
    if (
        not inchi_evidence
        or not smiles_evidence
        or inchi_evidence.get('assessment') != 'SUPPORTS'
    ):
        return None
    inchi_composition = _reported_composition_check(
        inchi_evidence,
        'inchi',
    )
    smiles_composition = _reported_composition_check(
        smiles_evidence,
        'canonical_smiles',
    )
    if not inchi_composition or not smiles_composition:
        return None
    inchi_checks = inchi_composition['reported_checks']
    smiles_checks = smiles_composition['reported_checks']
    if not (
        inchi_checks.get('formula') == 'match'
        and inchi_checks.get('molecular_weight') == 'match'
        and smiles_checks.get('formula') == 'mismatch'
        and smiles_checks.get('molecular_weight') == 'mismatch'
    ):
        return None

    inchi_flags = set(
        structure_rows.get(inchi_key, {}).get('review_flags', [])
    )
    smiles_flags = set(
        structure_rows.get(smiles_key, {}).get('review_flags', [])
    )
    if (
        inchi_flags != {'conflicting_structure_inputs'}
        or not smiles_flags
        or not smiles_flags <= {
            'conflicting_structure_inputs',
            'reported_formula_mismatch',
            'reported_molecular_weight_mismatch',
            'reported_exact_mass_mismatch',
        }
        or structure_rows.get(inchi_key, {}).get(
            'already_in_structure_database'
        )
        or structure_rows.get(smiles_key, {}).get(
            'already_in_structure_database'
        )
    ):
        return None

    independent_support = any(
        row.get('assessment') == 'SUPPORTS'
        and str(row.get('web_finding_id')) != str(finding_id)
        for row in evidence_by_structure.get(smiles_key, [])
    )
    if independent_support:
        return None
    return {
        'keys': [inchi_key, smiles_key],
        'finding_ids': [str(finding_id)],
    }


def _reported_inchi_counterion_pair(
    finding_evidence,
    structure_rows,
    evidence_by_structure,
    finding_id,
):
    return _reported_inchi_preferred_pair(
        finding_evidence,
        structure_rows,
        evidence_by_structure,
        finding_id,
        require_counterion_difference=True,
    )


def _reported_inchi_composition_pair(
    finding_evidence,
    structure_rows,
    evidence_by_structure,
    finding_id,
):
    return _reported_inchi_preferred_pair(
        finding_evidence,
        structure_rows,
        evidence_by_structure,
        finding_id,
    )


def _split_structure_input_candidates(evidence_rows, structure_rows):
    allowed_flags = {
        'conflicting_structure_inputs',
        'reported_formula_mismatch',
        'reported_molecular_weight_mismatch',
        'reported_exact_mass_mismatch',
    }
    candidates_by_pair = defaultdict(set)
    findings_by_pair = defaultdict(set)
    for evidence in evidence_rows:
        validations = evidence.get('validation') or []
        if isinstance(validations, dict):
            validations = [validations]
        for validation in validations:
            if not isinstance(validation, dict):
                continue
            if 'inchi_smiles_disagree' not in validation.get(
                'key_checks',
                [],
            ):
                continue
            derived_structures = validation.get('derived_structures', [])
            keys_by_source = defaultdict(set)
            structures_by_source = {}
            for derived in derived_structures:
                if not isinstance(derived, dict):
                    continue
                source = derived.get('source')
                key = _normalise_inchikey(derived.get('inchikey'))
                if (
                    source in {'inchi', 'canonical_smiles'}
                    and key in structure_rows
                ):
                    keys_by_source[source].add(key)
                    structures_by_source[source] = derived
            if (
                len(keys_by_source['inchi']) != 1
                or len(keys_by_source['canonical_smiles']) != 1
            ):
                continue
            if not _is_salt_component_count_mismatch(
                structures_by_source.get('inchi', {}),
                structures_by_source.get('canonical_smiles', {}),
            ):
                continue
            pair = frozenset({
                *keys_by_source['inchi'],
                *keys_by_source['canonical_smiles'],
            })
            if len(pair) != 2:
                continue
            finding_id = evidence.get('web_finding_id')
            if not finding_id:
                continue
            candidates_by_pair[pair].update(pair)
            findings_by_pair[pair].add(str(finding_id))

    if len(candidates_by_pair) != 1:
        return []
    pair, candidate_keys = next(iter(candidates_by_pair.items()))
    if any(
        'conflicting_structure_inputs' not in structure_rows[key].get(
            'review_flags',
            [],
        )
        or not set(structure_rows[key].get('review_flags', []))
        <= allowed_flags
        for key in candidate_keys
    ):
        return []
    parent_keys = {
        structure_rows[key].get('parent_inchikey') or key
        for key in candidate_keys
    }
    if len(parent_keys) != 1:
        return []
    return {
        'keys': sorted(candidate_keys),
        'finding_ids': sorted(findings_by_pair[pair]),
    }


def _auto_accept_clear_salt_pairs(run_id):
    structure_rows = {
        row['inchikey']: row
        for row in _iter_jsonl(get_report_path(run_id))
    }
    evidence_by_finding = defaultdict(list)
    for evidence in _iter_jsonl(get_evidence_report_path(run_id)):
        if evidence.get('web_finding_id'):
            evidence_by_finding[str(evidence['web_finding_id'])].append(
                evidence
            )
    split_candidates_by_pair = {}
    for finding_id, evidence_rows in evidence_by_finding.items():
        split_candidates = _split_structure_input_candidates(
            evidence_rows,
            structure_rows,
        )
        if not split_candidates:
            continue
        pair = tuple(split_candidates['keys'])
        candidate = split_candidates_by_pair.setdefault(
            pair,
            {'keys': list(pair), 'finding_ids': set()},
        )
        candidate['finding_ids'].add(finding_id)
    if not split_candidates_by_pair:
        return []

    overlapping_keys = {
        key
        for pair in split_candidates_by_pair
        for key in pair
        if sum(key in other_pair for other_pair in split_candidates_by_pair) > 1
    }
    with _root_lock():
        state = read_run_state(run_id)
        if state.get('database_commit'):
            return []
        triage = get_triage_state(run_id)
        accepted_keys = []
        for pair, split_candidates in split_candidates_by_pair.items():
            if overlapping_keys.intersection(pair):
                continue
            group_decisions = {
                triage['groups'].get(
                    _review_group_id(
                        structure_rows[key].get('review_flags', [])
                    ),
                    'UNDECIDED',
                )
                for key in pair
            }
            if group_decisions != {'UNDECIDED'}:
                continue
            existing_decisions = {
                triage['structures'].get(key, {}).get('decision')
                for key in pair
            }
            if existing_decisions == {'ACCEPTED'}:
                continue
            if not existing_decisions <= {None, 'UNDECIDED', 'ACCEPTED'}:
                continue
            finding_ids = sorted(split_candidates['finding_ids'])
            for key in pair:
                entry = triage['structures'].setdefault(key, {})
                entry['decision'] = 'ACCEPTED'
                entry['split_source_finding_ids'] = sorted({
                    *entry.get('split_source_finding_ids', []),
                    *finding_ids,
                })
                entry['split_peer_keys'] = list(pair)
                entry['split_auto_accepted'] = True
                accepted_keys.append(key)
        if not accepted_keys:
            return []
        _atomic_json(_triage_path(run_id), triage)
    return sorted(accepted_keys)


def _auto_triage_reported_inchi_counterion_pairs(run_id):
    structure_rows = {
        row['inchikey']: row
        for row in _iter_jsonl(get_report_path(run_id))
    }
    evidence_by_finding = defaultdict(list)
    evidence_by_structure = defaultdict(list)
    for evidence in _iter_jsonl(get_evidence_report_path(run_id)):
        if evidence.get('web_finding_id'):
            evidence_by_finding[str(evidence['web_finding_id'])].append(
                evidence
            )
        if evidence.get('structure_inchikey'):
            evidence_by_structure[evidence['structure_inchikey']].append(
                evidence
            )

    candidates_by_pair = {}
    for finding_id, finding_evidence in evidence_by_finding.items():
        candidate = _reported_inchi_counterion_pair(
            finding_evidence,
            structure_rows,
            evidence_by_structure,
            finding_id,
        )
        if not candidate:
            continue
        pair = tuple(candidate['keys'])
        pair_candidate = candidates_by_pair.setdefault(
            pair,
            {'keys': list(pair), 'finding_ids': set()},
        )
        pair_candidate['finding_ids'].update(candidate['finding_ids'])
    if not candidates_by_pair:
        return []

    overlapping_keys = {
        key
        for pair in candidates_by_pair
        for key in pair
        if sum(key in other_pair for other_pair in candidates_by_pair) > 1
    }
    with _root_lock():
        state = read_run_state(run_id)
        if state.get('database_commit'):
            return []
        triage = get_triage_state(run_id)
        triaged_keys = []
        for pair, candidate in candidates_by_pair.items():
            if overlapping_keys.intersection(pair):
                continue
            inchi_key, smiles_key = pair
            group_decisions = {
                triage['groups'].get(
                    _review_group_id(
                        structure_rows[key].get('review_flags', [])
                    ),
                    'UNDECIDED',
                )
                for key in pair
            }
            existing_decisions = {
                triage['structures'].get(key, {}).get('decision')
                for key in pair
            }
            if (
                group_decisions != {'UNDECIDED'}
                or not existing_decisions <= {None, 'UNDECIDED'}
            ):
                continue

            finding_ids = sorted(candidate['finding_ids'])
            inchi_entry = triage['structures'].setdefault(inchi_key, {})
            inchi_entry.update({
                'decision': 'ACCEPTED',
                'auto_decision_reason': (
                    'The reported InChIKey, formula, and molecular weight '
                    'match the InChI-parsed structure. The competing SMILES '
                    'differs only by a lower count of alkali or '
                    'alkaline-earth counterions.'
                ),
                'auto_decision_finding_ids': finding_ids,
                'auto_decision_peer_key': smiles_key,
                'auto_decision_rule': 'reported_inchi_counterion_count',
            })
            smiles_entry = triage['structures'].setdefault(smiles_key, {})
            smiles_entry.update({
                'decision': 'REJECTED',
                'auto_decision_reason': (
                    'Not selected: the reported InChIKey, formula, and '
                    'molecular weight match the InChI-parsed structure; '
                    'the competing SMILES differs only by a lower count of '
                    'alkali or alkaline-earth counterions.'
                ),
                'auto_decision_finding_ids': finding_ids,
                'auto_decision_peer_key': inchi_key,
                'auto_decision_rule': 'reported_inchi_counterion_count',
            })
            triaged_keys.extend(pair)

        if not triaged_keys:
            return []
        _atomic_json(_triage_path(run_id), triage)
    return sorted(set(triaged_keys))


def _auto_triage_reported_inchi_composition_pairs(run_id):
    structure_rows = {
        row['inchikey']: row
        for row in _iter_jsonl(get_report_path(run_id))
    }
    evidence_by_finding = defaultdict(list)
    evidence_by_structure = defaultdict(list)
    for evidence in _iter_jsonl(get_evidence_report_path(run_id)):
        if evidence.get('web_finding_id'):
            evidence_by_finding[str(evidence['web_finding_id'])].append(
                evidence
            )
        if evidence.get('structure_inchikey'):
            evidence_by_structure[evidence['structure_inchikey']].append(
                evidence
            )

    candidates_by_pair = {}
    for finding_id, finding_evidence in evidence_by_finding.items():
        candidate = _reported_inchi_composition_pair(
            finding_evidence,
            structure_rows,
            evidence_by_structure,
            finding_id,
        )
        if not candidate:
            continue
        pair = tuple(candidate['keys'])
        pair_candidate = candidates_by_pair.setdefault(
            pair,
            {'keys': list(pair), 'finding_ids': set()},
        )
        pair_candidate['finding_ids'].update(candidate['finding_ids'])
    if not candidates_by_pair:
        return []

    overlapping_keys = {
        key
        for pair in candidates_by_pair
        for key in pair
        if sum(key in other_pair for other_pair in candidates_by_pair) > 1
    }
    with _root_lock():
        state = read_run_state(run_id)
        if state.get('database_commit'):
            return []
        triage = get_triage_state(run_id)
        triaged_keys = []
        for pair, candidate in candidates_by_pair.items():
            if overlapping_keys.intersection(pair):
                continue
            inchi_key, smiles_key = pair
            group_decisions = {
                triage['groups'].get(
                    _review_group_id(
                        structure_rows[key].get('review_flags', [])
                    ),
                    'UNDECIDED',
                )
                for key in pair
            }
            existing_decisions = {
                triage['structures'].get(key, {}).get('decision')
                for key in pair
            }
            if (
                group_decisions != {'UNDECIDED'}
                or not existing_decisions <= {None, 'UNDECIDED'}
            ):
                continue

            finding_ids = sorted(candidate['finding_ids'])
            reason = (
                'The reported InChIKey, formula, and molecular weight match '
                'the InChI-parsed structure. The competing SMILES parses to '
                'a different key and disagrees with both reported '
                'composition values; no independent finding supports that '
                'alternative.'
            )
            for key, decision, peer_key in (
                (inchi_key, 'ACCEPTED', smiles_key),
                (smiles_key, 'REJECTED', inchi_key),
            ):
                entry = triage['structures'].setdefault(key, {})
                entry.update({
                    'decision': decision,
                    'auto_decision_reason': reason,
                    'auto_decision_finding_ids': finding_ids,
                    'auto_decision_peer_key': peer_key,
                    'auto_decision_rule': (
                        'reported_inchi_key_and_composition'
                    ),
                })
                triaged_keys.append(key)

        if not triaged_keys:
            return []
        _atomic_json(_triage_path(run_id), triage)
    return sorted(set(triaged_keys))


def _auto_triage_fractional_stoichiometry_mismatches(run_id):
    structure_rows = {
        row['inchikey']: row
        for row in _iter_jsonl(get_report_path(run_id))
    }
    evidence_by_structure = defaultdict(list)
    for evidence in _iter_jsonl(get_evidence_report_path(run_id)):
        if evidence.get('structure_inchikey'):
            evidence_by_structure[evidence['structure_inchikey']].append(
                evidence
            )

    eligible_keys = []
    for inchikey, structure in structure_rows.items():
        if structure.get('review_flags') != [
            'reported_salt_stoichiometry_mismatch',
        ]:
            continue
        evidence_rows = evidence_by_structure.get(inchikey, [])
        if not evidence_rows or any(
            evidence.get('assessment') != 'SUPPORTS'
            for evidence in evidence_rows
        ):
            continue

        has_reported_stoichiometry_conflict = False
        safe_evidence = True
        finding_ids = set()
        for evidence in evidence_rows:
            validations = evidence.get('validation') or []
            if isinstance(validations, dict):
                validations = [validations]
            if not validations:
                safe_evidence = False
                break
            finding_ids.add(str(evidence.get('web_finding_id')))
            for validation in validations:
                if not isinstance(validation, dict):
                    safe_evidence = False
                    break
                computed_keys = {
                    _normalise_inchikey(key)
                    for key in validation.get('computed_inchikeys', [])
                    if _normalise_inchikey(key)
                }
                reported_key = _normalise_inchikey(
                    validation.get('reported_inchikey')
                )
                key_checks = set(validation.get('key_checks', []))
                if (
                    computed_keys != {inchikey}
                    or 'inchi_smiles_disagree' in key_checks
                    or 'reported_derived_key_mismatch' in key_checks
                    or (
                        reported_key is not None
                        and reported_key != inchikey
                    )
                ):
                    safe_evidence = False
                    break

                derived_structures = validation.get(
                    'derived_structures',
                    [],
                )
                derived_by_source = defaultdict(set)
                for derived in derived_structures:
                    if isinstance(derived, dict) and derived.get('source'):
                        derived_by_source[derived['source']].add(
                            _normalise_inchikey(derived.get('inchikey'))
                        )
                if (
                    derived_by_source.get('inchi') != {inchikey}
                    or derived_by_source.get('canonical_smiles')
                    != {inchikey}
                ):
                    safe_evidence = False
                    break

                if validation.get('reported_stoichiometry_mismatch'):
                    if (
                        not validation.get('reported_inchikey_valid')
                        or reported_key != inchikey
                        or 'structure_key_match' not in key_checks
                    ):
                        safe_evidence = False
                        break
                    has_reported_stoichiometry_conflict = True
                else:
                    for computed in validation.get(
                        'computed_values_for_candidate',
                        [],
                    ):
                        checks = computed.get('reported_checks', {})
                        if any(
                            result in {'mismatch', 'unparseable'}
                            for result in checks.values()
                        ):
                            safe_evidence = False
                            break
                if not safe_evidence:
                    break
            if not safe_evidence:
                break
        if safe_evidence and has_reported_stoichiometry_conflict:
            eligible_keys.append((inchikey, sorted(finding_ids)))

    if not eligible_keys:
        return []
    with _root_lock():
        state = read_run_state(run_id)
        if state.get('database_commit'):
            return []
        triage = get_triage_state(run_id)
        accepted = []
        for inchikey, finding_ids in eligible_keys:
            group_id = _review_group_id(
                structure_rows[inchikey].get('review_flags', [])
            )
            if triage['groups'].get(group_id, 'UNDECIDED') != 'UNDECIDED':
                continue
            entry = triage['structures'].setdefault(inchikey, {})
            if entry.get('decision') not in {None, 'UNDECIDED'}:
                continue
            entry.update({
                'decision': 'ACCEPTED',
                'auto_decision_reason': (
                    'The provider InChIKey matches the parsed InChI and '
                    'SMILES, and all retained evidence supports this exact '
                    'structure. The reported fractional formula and '
                    'molecular weight agree with each other but not this '
                    'structure, so the accepted Structure keeps the '
                    'computed composition; original provider values remain '
                    'in the evidence.'
                ),
                'auto_decision_finding_ids': finding_ids,
                'auto_decision_rule': (
                    'fractional_stoichiometry_with_matching_structure'
                ),
            })
            accepted.append(inchikey)
        if not accepted:
            return []
        _atomic_json(_triage_path(run_id), triage)
    return sorted(accepted)


def get_review_queue(run_id):
    all_groups = {
        group['id']: group for group in get_review_groups(run_id)
    }
    committed_keys = set(
        read_run_state(run_id).get('committed_inchikeys', [])
    )
    groups = {}
    ready_new = []
    already_existing = []
    rejected = []
    for record in iter_triaged_preview(run_id):
        if record['inchikey'] in committed_keys:
            continue
        flags = record.get('original_review_flags', [])
        if record['triage_decision'] == 'REJECTED':
            rejected.append(record)
        elif flags and record['triage_decision'] in {
            'UNDECIDED',
            'NEEDS_WORK',
        }:
            group_id = _review_group_id(flags)
            group = groups.setdefault(
                group_id,
                dict(all_groups.get(group_id, {
                    'id': group_id,
                    'flags': flags,
                    'complexity': _review_complexity(flags),
                    'decision': 'UNDECIDED',
                })),
            )
            group['descriptions'] = [
                _review_flag_description(flag) for flag in flags
            ]
            group.setdefault('records', []).append(record)
        elif record.get('already_in_structure_database'):
            already_existing.append(record)
        else:
            ready_new.append(record)

    review_records = [
        record
        for group in groups.values()
        for record in group.get('records', [])
    ]
    evidence_by_key = {
        record['triage_original_inchikey']: {
            'relevant': [],
            'other': [],
            'all': [],
        }
        for record in review_records
    }
    if evidence_by_key:
        for evidence in _iter_jsonl(get_evidence_report_path(run_id)):
            evidence_rows = evidence_by_key.get(
                evidence['structure_inchikey']
            )
            if evidence_rows is None:
                continue
            evidence_rows['all'].append(evidence)
            bucket = (
                'relevant'
                if _is_review_relevant_evidence(evidence)
                else 'other'
            )
            if len(evidence_rows[bucket]) < 3:
                evidence_rows[bucket].append(evidence)
    for record in review_records:
        evidence_rows = evidence_by_key[record['triage_original_inchikey']]
        record['issue_evidence'] = (
            evidence_rows['relevant'] + evidence_rows['other']
        )[:3]
        record['issue_details'] = _build_issue_details(
            record,
            record['issue_evidence'],
        )
        record['issue_edit_fields'] = _issue_edit_fields(
            record.get('original_review_flags', [])
        )
        record['split_structure_input_keys'] = []
        record['show_split_structure_action'] = False
        split_candidates = _split_structure_input_candidates(
            evidence_rows['all'],
            {
                candidate['triage_original_inchikey']: candidate
                for candidate in review_records
            },
        )
        if split_candidates:
            record['split_structure_input_keys'] = split_candidates['keys']
            record['show_split_structure_action'] = True
    for group in groups.values():
        group['count'] = len(group['records'])

    return {
        'groups': list(groups.values()),
        'ready_new': ready_new,
        'already_existing': already_existing,
        'rejected': rejected,
        'needs_review_count': sum(
            len(group['records']) for group in groups.values()
        ),
    }


def _triage_record(record, triage):
    original_key = record['inchikey']
    group_id = _review_group_id(record.get('review_flags', []))
    original_flags = list(record.get('review_flags', []))
    group_decision = triage['groups'].get(group_id, 'UNDECIDED')
    individual = triage['structures'].get(original_key, {})
    if individual.get('edits'):
        record.update(individual['edits'])
    record['triage_group_id'] = group_id
    record['triage_decision'] = individual.get('decision')
    if record['triage_decision'] is None:
        record['triage_decision'] = (
            'ACCEPTED' if not record.get('review_flags') else group_decision
        )
    record['original_review_flags'] = original_flags
    record['triage_explicit_edit_fields'] = individual.get(
        'explicit_edit_fields',
        [],
    )
    record['triage_auto_reason'] = individual.get('auto_decision_reason')
    if record['triage_decision'] == 'ACCEPTED':
        record['review_flags'] = []
        record['review_status'] = 'SUPPORTED'
    record['triage_original_inchikey'] = original_key
    return record


def iter_triaged_preview(run_id):
    triage = get_triage_state(run_id)
    for record in _iter_jsonl(get_report_path(run_id)):
        yield _triage_record(record, triage)


def get_preview(run_id, limit=100):
    records = []
    for index, record in enumerate(iter_triaged_preview(run_id)):
        if limit is not None and index >= limit:
            break
        records.append(record)
    return records


def _ensure_commit_not_active(run_id, state):
    if _commit_job_is_active(run_id, state):
        raise ValueError(
            'Accepted structures are being added to the database. Wait for '
            'that to finish before changing this review.'
        )


def save_group_decision(run_id, group_id, decision):
    if decision not in TRIAGE_DECISIONS:
        raise ValueError('Choose an allowed review decision.')
    with _root_lock():
        state = read_run_state(run_id)
        if state.get('database_commit'):
            raise ValueError('This preview has already been added to the database.')
        _ensure_commit_not_active(run_id, state)
        if not any(
            group['id'] == group_id
            for group in state.get('review_groups', [])
        ):
            raise ValueError('That review group is not part of this scan.')
        triage = get_triage_state(run_id)
        triage['groups'][group_id] = decision
        _atomic_json(_triage_path(run_id), triage)
    _sync_batch_finding_statuses(run_id)
    return decision


def save_structure_decision(
    run_id,
    inchikey,
    decision,
    smiles=None,
    field_edits=None,
):
    if decision not in TRIAGE_DECISIONS:
        raise ValueError('Choose an allowed review decision.')
    inchikey = _normalise_inchikey(inchikey)
    if not inchikey or not INCHIKEY_PATTERN.fullmatch(inchikey):
        raise ValueError('The selected structure key is invalid.')
    structure_row = None
    existing_keys = set()
    for row in _iter_jsonl(get_report_path(run_id)):
        if row['inchikey'] == inchikey:
            structure_row = row
        else:
            existing_keys.add(row['inchikey'])
    if structure_row is None:
        raise ValueError('That structure is not part of this scan.')

    edits = None
    field_edits = field_edits or {}
    unknown_fields = set(field_edits) - EDITABLE_CHEMISTRY_FIELDS
    if unknown_fields:
        raise ValueError('One or more chemistry fields cannot be edited.')
    structure_row['edited_smiles'] = smiles.strip() if smiles else None
    if smiles or field_edits:
        if smiles:
            structure_row = _run_rdkit_for_row(structure_row)
            if structure_row.get('worker_error'):
                raise ValueError(structure_row['worker_error'])

        chemistry_edits = {}
        if 'molecular_formula' in field_edits:
            formula_value = str(field_edits['molecular_formula']).strip()
            normalized_formula = normalize_formula(formula_value)
            if formula_value and normalized_formula is None:
                raise ValueError(
                    'Enter a valid molecular formula using element symbols '
                    'and counts.'
                )
            if formula_value:
                chemistry_edits['molecular_formula'] = normalized_formula

        for field in ('molecular_weight', 'exact_mass'):
            if field not in field_edits:
                continue
            value = str(field_edits[field]).strip()
            if not value:
                continue
            number = _number(value)
            if number is None:
                raise ValueError(f'Enter a valid numeric value for {field}.')
            chemistry_edits[field] = number
        structure_row.update(chemistry_edits)

        edited_key = structure_row['inchikey']
        if edited_key in existing_keys:
            raise ValueError(
                'This edited structure matches another preview row. '
                'Resolve the duplicate group before applying the edit.'
            )
        edits = {
            field: value
            for field, value in structure_row.items()
            if field in {
                'inchikey',
                *STRUCTURE_DRAFT_FIELDS,
                'parent_id',
                'parent_inchikey',
                'parent_is_self',
                'parent_generation_status',
                'parent_generation_error',
                'components',
                'review_flags',
                'review_status',
            }
        }

    with _root_lock():
        state = read_run_state(run_id)
        if state.get('database_commit'):
            raise ValueError('This preview has already been added to the database.')
        _ensure_commit_not_active(run_id, state)
        triage = get_triage_state(run_id)
        entry = triage['structures'].setdefault(inchikey, {})
        entry['decision'] = decision
        for field in (
            'auto_decision_reason',
            'auto_decision_finding_ids',
            'auto_decision_peer_key',
            'auto_decision_rule',
        ):
            entry.pop(field, None)
        explicit_edit_fields = {
            field
            for field, value in field_edits.items()
            if field in STRUCTURE_DRAFT_FIELDS
            and str(value).strip()
        }
        if smiles:
            explicit_edit_fields.update(STRUCTURE_DRAFT_FIELDS)
        entry['explicit_edit_fields'] = sorted(
            set(entry.get('explicit_edit_fields', []))
            | explicit_edit_fields
        )
        if edits is not None:
            entry['edits'] = edits
        if decision == 'ACCEPTED':
            entry['resolved_flags'] = list(
                structure_row.get('review_flags', [])
            )
        _atomic_json(_triage_path(run_id), triage)
        if edits is not None and smiles:
            _refresh_triaged_parent_report(run_id)
    _sync_batch_finding_statuses(run_id)
    return decision


def save_structure_input_split(run_id, inchikey):
    inchikey = _normalise_inchikey(inchikey)
    if not inchikey or not INCHIKEY_PATTERN.fullmatch(inchikey):
        raise ValueError('The selected structure key is invalid.')

    with _root_lock():
        state = read_run_state(run_id)
        if state.get('database_commit'):
            raise ValueError(
                'This preview has already been added to the database.'
            )
        _ensure_commit_not_active(run_id, state)
        structure_rows = {
            row['inchikey']: row
            for row in _iter_jsonl(get_report_path(run_id))
        }
        if inchikey not in structure_rows:
            raise ValueError('That structure is not part of this scan.')
        evidence_rows = [
            row
            for row in _iter_jsonl(get_evidence_report_path(run_id))
            if row['structure_inchikey'] == inchikey
        ]
        split_candidates = _split_structure_input_candidates(
            evidence_rows,
            structure_rows,
        )
        if not split_candidates or inchikey not in split_candidates['keys']:
            raise ValueError(
                'This finding does not have exactly two safe, parsed '
                'structure candidates to accept separately.'
            )

        triage = get_triage_state(run_id)
        for key in split_candidates['keys']:
            entry = triage['structures'].setdefault(key, {})
            entry['decision'] = 'ACCEPTED'
            entry['split_source_finding_ids'] = sorted({
                *entry.get('split_source_finding_ids', []),
                *split_candidates['finding_ids'],
            })
            entry['split_peer_keys'] = split_candidates['keys']
        _atomic_json(_triage_path(run_id), triage)

    _sync_batch_finding_statuses(run_id)
    return split_candidates['keys']


def iter_triaged_evidence(run_id):
    triage = get_triage_state(run_id)
    edited_keys = {
        old_key: edits.get('inchikey', old_key)
        for old_key, entry in triage['structures'].items()
        if (edits := entry.get('edits'))
    }
    for evidence in _iter_jsonl(get_evidence_report_path(run_id)):
        evidence['structure_inchikey'] = edited_keys.get(
            evidence['structure_inchikey'],
            evidence['structure_inchikey'],
        )
        yield evidence


def get_report_path(run_id):
    return _run_dir(run_id) / 'preview.jsonl'


def has_current_preview_reports(run_id, state=None):
    state = state or read_run_state(run_id)
    return (
        state.get('report_format_version') == REPORT_FORMAT_VERSION
        and get_report_path(run_id).is_file()
        and get_parent_report_path(run_id).is_file()
        and get_evidence_report_path(run_id).is_file()
        and get_invalid_report_path(run_id).is_file()
    )


def get_invalid_report_path(run_id):
    return _run_dir(run_id) / 'invalid.jsonl'


def get_evidence_report_path(run_id):
    return _run_dir(run_id) / 'structure_evidence.jsonl'


def get_parent_report_path(run_id):
    return _run_dir(run_id) / 'parent_structures.jsonl'


def get_parent_preview(run_id, limit=100):
    parent_path = get_parent_report_path(run_id)
    if not parent_path.exists():
        return []
    return [
        record for _, record in zip(range(limit), _iter_jsonl(parent_path))
    ]


def _refresh_triaged_parent_report(run_id):
    target_path = get_parent_report_path(run_id)
    with tempfile.TemporaryDirectory(dir=target_path.parent) as temporary:
        temporary_dir = Path(temporary)
        source_path = temporary_dir / 'triaged.jsonl'
        output_path = temporary_dir / 'preview.jsonl'
        parent_path = temporary_dir / 'parent_structures.jsonl'
        with source_path.open('w', encoding='utf-8') as source_file:
            for row in iter_triaged_preview(run_id):
                source_file.write(json.dumps(row, ensure_ascii=False) + '\n')
            source_file.flush()
            os.fsync(source_file.fileno())
        _run_rdkit_parent_worker(source_path, output_path, parent_path)
        _mark_parent_database_presence(parent_path)
        os.replace(parent_path, target_path)


def _commit_job_is_active(run_id, state):
    job = state.get('commit_job') or {}
    status = job.get('status')
    if status == 'QUEUED':
        return True
    if status == 'RUNNING':
        try:
            with run_lock(run_id):
                return False
        except RuntimeError:
            return True
    return False


def _set_commit_job(run_id, status, error=None):
    state = read_run_state(run_id)
    state['commit_job'] = {
        'status': status,
        'error': error,
        'updated_at': timezone.now().isoformat(),
    }
    _write_state(run_id, state)


def _commit_progress_writer(run_id):
    def report(stage, completed=0, total=0):
        _set_commit_progress(run_id, stage, completed, total)
    return report


def queue_reviewed_preview_commit(run_id):
    with _root_lock():
        state = read_run_state(run_id)
        if (
            state.get('status') != 'COMPLETED'
            or not has_current_preview_reports(run_id, state)
        ):
            raise ValueError('A current completed preview is required.')
        if state.get('database_commit'):
            return False
        if _commit_job_is_active(run_id, state):
            return False
        timestamp = timezone.now().isoformat()
        state['commit_job'] = {
            'status': 'QUEUED',
            'error': None,
            'updated_at': timestamp,
        }
        state['commit_progress'] = {
            'stage': 'Queued',
            'completed': 0,
            'total': 0,
            'updated_at': timestamp,
        }
        _write_state(run_id, state)
    return True


def run_reviewed_preview_commit(run_id):
    with run_lock(run_id):
        _set_commit_job(run_id, 'RUNNING')
        try:
            commit_summary = commit_reviewed_preview(
                run_id,
                progress=_commit_progress_writer(run_id),
            )
        except Exception as exc:
            _set_commit_job(run_id, 'FAILED', error=str(exc))
            raise
        _set_commit_job(run_id, 'COMPLETED')
    return commit_summary


def _ignore_commit_progress(stage, completed=0, total=0):
    return None


def _set_commit_progress(run_id, stage, completed=0, total=0):
    state = read_run_state(run_id)
    state['commit_progress'] = {
        'stage': stage,
        'completed': completed,
        'total': total,
        'updated_at': timezone.now().isoformat(),
    }
    _write_state(run_id, state)


def _write_in_chunks(items, write_chunk, progress, stage):
    total = len(items)
    completed = 0
    progress(stage, completed, total)
    for start in range(0, total, CHUNK_SIZE):
        chunk = items[start:start + CHUNK_SIZE]
        write_chunk(chunk)
        completed += len(chunk)
        progress(stage, completed, total)


def commit_reviewed_preview(run_id, progress=None):
    progress = progress or _ignore_commit_progress
    with _root_lock():
        state = read_run_state(run_id)
        if (
            state.get('status') != 'COMPLETED'
            or not has_current_preview_reports(run_id, state)
        ):
            raise ValueError('A current completed preview is required.')
        if state.get('database_commit'):
            return state['database_commit']

        batch = (
            StructureCurationBatch.objects.get(pk=state['batch_id'])
            if state.get('batch_id')
            else None
        )
        if batch and batch.status == StructureCurationBatch.Status.SUPERSEDED:
            raise ValueError(
                'This curation batch was superseded and cannot be committed.'
            )

        records = list(iter_triaged_preview(run_id))
        accepted = [
            row for row in records
            if row.get('triage_decision') == 'ACCEPTED'
        ]
        if not accepted:
            has_unresolved_batch_findings = (
                batch.findings.filter(
                    status__in=[
                        StructureCurationBatchFinding.Status.SELECTED,
                        StructureCurationBatchFinding.Status.NEEDS_WORK,
                    ]
                ).exists()
                if batch
                else False
            )
            if batch and not has_unresolved_batch_findings:
                commit_summary = {
                    'committed_at': timezone.now().isoformat(),
                    'accepted_structures': 0,
                    'new_structure_rows': 0,
                    'new_parent_rows': 0,
                    'evidence_links_processed': 0,
                    'identifiers_linked': 0,
                }
                batch.status = StructureCurationBatch.Status.COMMITTED
                batch.committed_at = timezone.now()
                batch.save(update_fields=['status', 'committed_at'])
                state['database_commit'] = commit_summary
                state['updated_at'] = commit_summary['committed_at']
                _write_state(run_id, state)
                return commit_summary
            raise ValueError('There are no accepted structures ready to add.')
        committed_keys = set(state.get('committed_inchikeys', []))
        accepted_to_commit = [
            row for row in accepted
            if row['inchikey'] not in committed_keys
        ]
        if not accepted_to_commit:
            raise ValueError(
                'There are no newly accepted structures to add. Resolve '
                'remaining deferred records or finish this batch.'
            )
        if any(
            row.get('parent_generation_status') == 'FAILED'
            for row in accepted
        ):
            raise ValueError(
                'Resolve parent-generation failures before adding accepted '
                'structures to the database.'
            )

        accepted_to_commit_keys = {
            row['inchikey'] for row in accepted_to_commit
        }
        all_parent_records = {
            row['inchikey']: row
            for row in _iter_jsonl(get_parent_report_path(run_id))
        }
        parent_keys = {
            row['parent_inchikey'] for row in accepted_to_commit
        }
        parent_records = {
            key: row
            for key, row in all_parent_records.items()
            if key in parent_keys
        }
        missing_parent_keys = parent_keys - parent_records.keys()
        if missing_parent_keys:
            raise ValueError(
                'One or more accepted structures have no generated parent '
                'record. Correct their structures before committing.'
            )

        structure_values = dict(parent_records)
        component_values = {}
        for row in accepted_to_commit:
            for component in row.get('components', []):
                component_values.setdefault(component['inchikey'], component)
        structure_values.update(component_values)
        for row in accepted_to_commit:
            structure_values.setdefault(row['inchikey'], row)
        all_keys = set(structure_values)
        evidence_rows = [
            evidence for evidence in iter_triaged_evidence(run_id)
            if evidence['structure_inchikey'] in accepted_to_commit_keys
        ]
        finding_ids = {row['web_finding_id'] for row in evidence_rows}
        findings = {
            str(pk): finding
            for pk, finding in WebFinding.objects.in_bulk(finding_ids).items()
        }
        missing_findings = finding_ids - findings.keys()
        if missing_findings:
            raise ValueError(
                'Some source findings were removed after the scan. Run a '
                'new scan before committing this preview.'
            )

        with transaction.atomic():
            structures = Structure.objects.in_bulk(
                all_keys,
                field_name='inchikey',
            )
            existing_keys = set(structures)
            new_structures = [
                    Structure(
                        inchikey=key,
                        inchi=value.get('inchi'),
                        canonical_smiles=value.get('canonical_smiles'),
                        isomeric_smiles=value.get('isomeric_smiles'),
                        molecular_formula=value.get('molecular_formula'),
                        molecular_weight=value.get('molecular_weight'),
                        exact_mass=value.get('exact_mass'),
                        standardization_toolkit=(
                            value.get('standardization_toolkit') or ''
                        ),
                        standardization_version=(
                            value.get('standardization_version') or ''
                        ),
                        standardization_policy=(
                            value.get('standardization_policy') or ''
                        ),
                    )
                    for key, value in structure_values.items()
                    if key not in existing_keys
                ]
            _write_in_chunks(
                new_structures,
                lambda chunk: Structure.objects.bulk_create(
                    chunk,
                    ignore_conflicts=True,
                ),
                progress,
                'Adding structures',
            )
            structures = Structure.objects.in_bulk(
                all_keys,
                field_name='inchikey',
            )
            accepted_fields = (
                'inchi',
                'canonical_smiles',
                'isomeric_smiles',
                'molecular_formula',
                'molecular_weight',
                'exact_mass',
                'standardization_toolkit',
                'standardization_version',
                'standardization_policy',
            )
            accepted_structures = []
            for row in accepted_to_commit:
                structure = structures[row['inchikey']]
                explicit_edit_fields = set(
                    row.get('triage_explicit_edit_fields', [])
                )
                for field in accepted_fields:
                    if (
                        row['inchikey'] in existing_keys
                        and field not in explicit_edit_fields
                        and getattr(structure, field) not in (None, '')
                    ):
                        continue
                    value = row.get(field)
                    if value is not None and value != '':
                        setattr(structure, field, value)
                accepted_structures.append(structure)
            for key, parent_row in parent_records.items():
                if key in structures and key in parent_keys:
                    structures[key].parent = structures[
                        parent_row['parent_inchikey']
                    ]
            for row in accepted_to_commit:
                structures[row['inchikey']].parent = structures[
                    row['parent_inchikey']
                ]
            _write_in_chunks(
                list(structures.values()),
                lambda chunk: Structure.objects.bulk_update(
                    chunk,
                    ['parent'],
                ),
                progress,
                'Linking parent structures',
            )
            _write_in_chunks(
                accepted_structures,
                lambda chunk: Structure.objects.bulk_update(
                    chunk,
                    accepted_fields,
                ),
                progress,
                'Updating accepted structures',
            )

            component_links = []
            for row in accepted_to_commit:
                for component in row.get('components', []):
                    component_key = component['inchikey']
                    if component_key == row['inchikey']:
                        continue
                    component_links.append(StructureComponent(
                        structure=structures[row['inchikey']],
                        component=structures[component_key],
                        position=component['position'],
                        count=component.get('component_count', 1),
                        is_parent_component=component.get(
                            'is_parent_component',
                            component_key == row['parent_inchikey'],
                        ),
                    ))
            _write_in_chunks(
                component_links,
                lambda chunk: StructureComponent.objects.bulk_create(
                    chunk,
                    ignore_conflicts=True,
                ),
                progress,
                'Linking components',
            )

            evidence_objects = [
                StructureEvidence(
                    structure=structures[row['structure_inchikey']],
                    web_finding=findings[row['web_finding_id']],
                    assessment=row['assessment'],
                    reported_values=row['reported_values'],
                    validation=row['validation'],
                )
                for row in evidence_rows
            ]
            _write_in_chunks(
                evidence_objects,
                lambda chunk: StructureEvidence.objects.bulk_create(
                    chunk,
                    ignore_conflicts=True,
                ),
                progress,
                'Saving evidence',
            )
            progress('Linking identifiers')
            identifier_summary = link_verified_identifiers(
                search_term_ids=get_search_term_ids_for_findings(finding_ids),
            )

        _sync_batch_finding_statuses(run_id)
        committed_keys.update(row['inchikey'] for row in accepted_to_commit)
        state['committed_inchikeys'] = sorted(committed_keys)
        commit_summary = {
            'committed_at': timezone.now().isoformat(),
            'accepted_structures': len(accepted_to_commit),
            'new_structure_rows': len(all_keys - existing_keys),
            'new_parent_rows': len(parent_keys - existing_keys),
            'evidence_links_processed': len(evidence_rows),
            'identifiers_linked': identifier_summary['created'],
        }
        state['database_commit_partial'] = commit_summary
        unresolved = (
            batch.findings.filter(
                status__in=[
                    StructureCurationBatchFinding.Status.SELECTED,
                    StructureCurationBatchFinding.Status.NEEDS_WORK,
                ]
            ).exists()
            if batch
            else False
        )
        state['updated_at'] = commit_summary['committed_at']
        if not unresolved:
            state['database_commit'] = commit_summary
            if batch:
                batch.status = StructureCurationBatch.Status.COMMITTED
                batch.committed_at = timezone.now()
                batch.save(update_fields=['status', 'committed_at'])
        _write_state(run_id, state)
        return commit_summary


def get_reviewed_structure_report_path(run_id):
    return _run_dir(run_id) / 'triaged-structures.jsonl'


def get_reviewed_evidence_report_path(run_id):
    return _run_dir(run_id) / 'triaged-evidence.jsonl'


def get_invalid_preview(run_id, limit=50):
    invalid_path = get_invalid_report_path(run_id)
    if not invalid_path.exists():
        return []
    return [
        record for _, record in zip(range(limit), _iter_jsonl(invalid_path))
    ]


def get_evidence_preview(run_id, limit=100):
    return [
        record for _, record in zip(
            range(limit),
            iter_triaged_evidence(run_id),
        )
    ]


def delete_run(run_id):
    with _root_lock() as root:
        run_dir = _run_dir(run_id)
        state = read_run_state(run_id)
        if state['status'] in {'QUEUED', 'RUNNING', 'FINALIZING'}:
            raise ValueError('An active curation scan cannot be deleted.')
        _ensure_commit_not_active(run_id, state)
        if state.get('batch_id'):
            raise ValueError(
                'Saved WebFinding batches cannot be deleted; keep them so '
                'deferred review can be resumed.'
            )
        shutil.rmtree(run_dir)
        latest_path = root / 'latest_run.json'
        if latest_path.exists():
            latest = json.loads(latest_path.read_text(encoding='utf-8'))
            if latest.get('id') == str(uuid.UUID(str(run_id))):
                latest_path.unlink()


def parse_finding(finding):
    task = finding.search_task
    source = task.web_source if task else None
    provider_code = (
        (source.provider_code or '').strip().lower()
        if source else ''
    )
    parser = _parser_for_provider(provider_code)
    if parser is None:
        return []

    records, _stats = parser([finding])
    return records


def normalize_inchikey(value):
    return _normalise_inchikey(value)


def _structure_draft_values(inchikey, computed_values):
    return {
        'inchikey': inchikey,
        'inchi': computed_values.get('inchi'),
        'canonical_smiles': computed_values.get('canonical_smiles'),
        'isomeric_smiles': computed_values.get('isomeric_smiles'),
        'molecular_formula': computed_values.get('formula'),
        'molecular_weight': computed_values.get('molecular_weight'),
        'exact_mass': computed_values.get('exact_mass'),
        'heavy_atom_count': None,
        'fragment_count': None,
        'parent_id': None,
        'standardization_toolkit': computed_values.get('toolkit') or '',
        'standardization_version': computed_values.get('toolkit_version') or '',
        'standardization_policy': computed_values.get('policy') or '',
    }


def build_finding_drafts(finding):
    provider_code = (
        (finding.search_task.web_source.provider_code or '').strip().lower()
        if finding.search_task_id else ''
    )
    drafts = []
    rejected = []
    records = parse_finding(finding)

    for record in records:
        record = dict(record)
        record.update({
            'provider_code': provider_code,
            'finding_id': str(finding.pk),
            'finding_updated_at': finding.updated_at.isoformat(),
            'source_url': finding.source_url,
            'target_representation': (
                finding.search_task.target_representation
                if finding.search_task_id else None
            ),
        })
        candidate, invalid = validate_candidate(record)
        reported_key = (
            candidate['reported_inchikey']
            if candidate
            else normalize_inchikey(record.get('inchikey'))
        )
        reported_key_valid = bool(
            reported_key and INCHIKEY_PATTERN.fullmatch(reported_key)
        )
        derived_values = (
            candidate['validation']['derived_structures']
            if candidate else []
        )
        if invalid and not derived_values:
            derived_values = invalid.get('derived_structures', [])
        derived_by_key = defaultdict(list)
        for value in derived_values:
            derived_by_key[value['inchikey']].append(value)

        candidate_keys = _prefer_stereo_specific_keys(derived_by_key)
        if reported_key_valid:
            candidate_keys.add(reported_key)
            candidate_keys = _prefer_stereo_specific_keys(candidate_keys)
        if not candidate_keys:
            rejected.append({
                'finding_id': str(finding.pk),
                'provider_code': provider_code,
                'source_url': finding.source_url,
                'reason': (
                    invalid['reason']
                    if invalid
                    else 'no_inchikey_or_derivable_structure'
                ),
                'reported_inchikey': reported_key,
                'structure_errors': (
                    invalid.get('structure_errors', [])
                    if invalid else []
                ),
                'reported_values': record,
            })
            continue

        validation = (
            candidate['validation']
            if candidate else {
                'key_checks': (
                    ['inchi_smiles_disagree']
                    if len(derived_by_key) > 1
                    else ['reported_key_invalid']
                ),
                'derived_structures': derived_values,
                'structure_errors': (
                    invalid.get('structure_errors', [])
                    if invalid else []
                ),
            }
        )
        for key in sorted(candidate_keys):
            matching_structures = derived_by_key.get(key, [])
            if reported_key_valid and key == reported_key:
                if matching_structures:
                    assessment = 'SUPPORTS'
                elif all(
                    _is_stereo_unspecified_variant(key, derived_key)
                    for derived_key in derived_by_key
                ):
                    assessment = 'INDETERMINATE'
                else:
                    assessment = (
                        'CONFLICTS' if derived_values else 'INDETERMINATE'
                    )
            elif matching_structures:
                assessment = 'SUPPORTS'
            else:
                assessment = 'INDETERMINATE'

            computed_values = (
                matching_structures[0] if matching_structures else {}
            )
            structure = _structure_draft_values(key, computed_values)
            drafts.append({
                'structure': structure,
                'evidence': {
                    'structure_inchikey': key,
                    'web_finding_id': str(finding.pk),
                    'provider_code': provider_code,
                    'source_url': finding.source_url,
                    'finding_updated_at': finding.updated_at.isoformat(),
                    'target_representation': (
                        finding.search_task.target_representation
                        if finding.search_task_id else None
                    ),
                    'assessment': assessment,
                    'reported_values': record,
                    'validation': {
                        **validation,
                        'reported_inchikey': reported_key,
                        'reported_inchikey_valid': reported_key_valid,
                        'computed_inchikeys': sorted(derived_by_key),
                        'computed_values_for_candidate': matching_structures,
                    },
                },
                'computed_values': matching_structures,
            })
    return drafts, rejected
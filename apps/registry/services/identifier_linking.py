import re
from collections import defaultdict

from django.db import transaction
from django.db.models import Q

from apps.registry.models import (
    Identifier,
    SearchTerm,
    StructureEvidence,
    StructureIdentifier,
)
from apps.registry.services.cas_validator import detect_and_suggest_cas


IDENTIFIER_TYPE_ALIASES = {
    'CAS': Identifier.IdentifierType.CAS,
    'CASRN': Identifier.IdentifierType.CAS,
    'CAS_RN': Identifier.IdentifierType.CAS,
    'CAS-RN': Identifier.IdentifierType.CAS,
    'DTXSID': Identifier.IdentifierType.DTXSID,
    'PUBCHEM_CID': Identifier.IdentifierType.PUBCHEM_CID,
    'PUBCHEM CID': Identifier.IdentifierType.PUBCHEM_CID,
    'CID': Identifier.IdentifierType.PUBCHEM_CID,
    'CERAPP': Identifier.IdentifierType.CERAPP,
    'EINECS': Identifier.IdentifierType.EINECS,
    'EINECS NUMBER': Identifier.IdentifierType.EINECS,
    'UNII': Identifier.IdentifierType.UNII,
    'FDA UNII': Identifier.IdentifierType.UNII,
    'CHEMSPIDER': Identifier.IdentifierType.CHEMSPIDER,
    'CHEMSPIDER_ID': Identifier.IdentifierType.CHEMSPIDER,
}
SEARCH_TERM_TYPES = tuple(IDENTIFIER_TYPE_ALIASES)
DTXSID_PATTERN = re.compile(r'^DTXSID\d+$', re.IGNORECASE)


def _identifier_type(term_type):
    normalized = str(term_type or '').strip().upper().replace('-', '_')
    return IDENTIFIER_TYPE_ALIASES.get(normalized)


def _identifier_value(identifier_type, term_value):
    value = str(term_value or '').strip()
    if not value:
        return None
    if identifier_type == Identifier.IdentifierType.CAS:
        result = detect_and_suggest_cas(value)
        return result['suggestion'] if result.get('suggestion') else None
    if identifier_type == Identifier.IdentifierType.PUBCHEM_CID:
        value = re.sub(r'^CID:\s*', '', value, flags=re.IGNORECASE)
        return value if value.isdigit() else None
    if identifier_type == Identifier.IdentifierType.DTXSID:
        return value.upper() if DTXSID_PATTERN.fullmatch(value) else None
    return value


def _supported_identifier_pairs(search_terms):
    supported_types = set(Identifier.IdentifierType.values)
    evidence_by_pair = defaultdict(lambda: {
        'supporting_structure_ids': set(),
    })
    for term_type, term_value, structure_id, assessment in search_terms:
        identifier_type = _identifier_type(term_type)
        if identifier_type not in supported_types:
            continue
        value = _identifier_value(identifier_type, term_value)
        if value is None:
            continue
        pair = (identifier_type, value)
        evidence = evidence_by_pair[pair]
        if assessment == StructureEvidence.Assessment.SUPPORTS:
            evidence['supporting_structure_ids'].add(structure_id)

    return {
        pair: evidence['supporting_structure_ids']
        for pair, evidence in evidence_by_pair.items()
        if evidence['supporting_structure_ids']
    }


@transaction.atomic
def link_verified_identifiers(search_term_ids=None, apply=True):
    term_type_filter = Q()
    for term_type in SEARCH_TERM_TYPES:
        term_type_filter |= Q(
            web_finding__search_task__search_term__term_type__iexact=term_type,
        )
    evidence = StructureEvidence.objects.filter(term_type_filter)
    if search_term_ids is not None:
        if not search_term_ids:
            return {
                'created': 0,
                'would_create': 0,
                'already_linked': 0,
                'multi_structure_values': 0,
            }
        evidence = evidence.filter(
            web_finding__search_task__search_term_id__in=search_term_ids,
        )
    evidence_rows = evidence.values_list(
        'web_finding__search_task__search_term__term_type',
        'web_finding__search_task__search_term__term_value',
        'structure_id',
        'assessment',
    ).iterator(chunk_size=2000)
    candidate_pairs = _supported_identifier_pairs(evidence_rows)
    if not candidate_pairs:
        return {
            'created': 0,
            'would_create': 0,
            'already_linked': 0,
            'multi_structure_values': 0,
        }

    identifier_types = {identifier_type for identifier_type, _ in candidate_pairs}
    values = {value for _, value in candidate_pairs}
    existing_identifiers = {}
    for identifier_type, value, identifier_id in Identifier.objects.filter(
        identifier_type__in=identifier_types,
        identifier_value__in=values,
    ).values_list('identifier_type', 'identifier_value', 'pk'):
        existing_identifiers[(identifier_type, value)] = identifier_id

    missing_pairs = candidate_pairs.keys() - existing_identifiers.keys()
    if apply and missing_pairs:
        Identifier.objects.bulk_create(
            [
                Identifier(
                    identifier_type=identifier_type,
                    identifier_value=value,
                )
                for identifier_type, value in missing_pairs
            ],
            ignore_conflicts=True,
            batch_size=2000,
        )
        existing_identifiers.update({
            (identifier_type, value): identifier_id
            for identifier_type, value, identifier_id in Identifier.objects.filter(
                identifier_type__in=identifier_types,
                identifier_value__in=values,
            ).values_list('identifier_type', 'identifier_value', 'pk')
        })

    desired_links = {
        (identifier_id, structure_id)
        for pair, structure_ids in candidate_pairs.items()
        if (identifier_id := existing_identifiers.get(pair)) is not None
        for structure_id in structure_ids
    }
    if desired_links:
        existing_links = set(
            StructureIdentifier.objects.filter(
                identifier_id__in={
                    identifier_id for identifier_id, _ in desired_links
                },
                structure_id__in={
                    structure_id for _, structure_id in desired_links
                },
            ).values_list('identifier_id', 'structure_id')
        ) & desired_links
    else:
        existing_links = set()
    would_create = (
        sum(len(structure_ids) for structure_ids in candidate_pairs.values())
        - len(existing_links)
    )
    new_links = desired_links - existing_links
    if apply and new_links:
        StructureIdentifier.objects.bulk_create(
            [
                StructureIdentifier(
                    identifier_id=identifier_id,
                    structure_id=structure_id,
                )
                for identifier_id, structure_id in new_links
            ],
            ignore_conflicts=True,
            batch_size=2000,
        )

    return {
        'created': would_create if apply else 0,
        'would_create': would_create,
        'already_linked': len(existing_links),
        'multi_structure_values': sum(
            len(structure_ids) > 1
            for structure_ids in candidate_pairs.values()
        ),
    }


def get_search_term_ids_for_findings(finding_ids):
    return set(
        SearchTerm.objects.filter(
            tasks__findings__pk__in=finding_ids,
        ).values_list('pk', flat=True)
    )

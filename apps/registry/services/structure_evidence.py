# apps/registry/services/structure_evidence.py
from django.db.models import Count

from apps.registry.models import StructureEvidence


def get_candidate_support(term_ids):
    return (
        StructureEvidence.objects
        .filter(
            web_finding__search_task__search_term_id__in=term_ids,
            assessment=StructureEvidence.Assessment.SUPPORTS,
        )
        .values(
            'web_finding__search_task__search_term_id',
            'structure_id',
            'structure__inchikey',
        )
        .annotate(
            finding_count=Count('web_finding_id', distinct=True),
            provider_count=Count(
                'web_finding__search_task__web_source_id',
                distinct=True,
            ),
        )
        .order_by(
            'web_finding__search_task__search_term_id',
            '-provider_count',
            '-finding_count',
        )
    )
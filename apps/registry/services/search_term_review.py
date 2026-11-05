from collections import Counter

from django.db import transaction
from django.db.models import F
from django.db.models.functions import Lower

from apps.registry.models import (
    SearchTerm,
    SearchTermReview,
    SearchTask,
    WebSource,
)
from apps.registry.services.cas_validator import (
    detect_and_suggest_cas,
    validate_cas_checksum,
)
from apps.registry.services.task_generator import (
    _is_valid_source_pairing,
    _representations_for_source,
)


MAX_REVIEW_TERM_LENGTH = 500


def queue_unmatched_search_terms(payload, user):
    submission_counts = Counter()
    row_candidates = []
    candidate_values = set()
    for result in payload['results']:
        candidates = []
        for item in result.get('unmatched_inputs', ()):
            term_value = str(item['value']).strip()
            term_type = str(item['type']).strip().upper()
            if not term_value or not term_type:
                continue
            if len(term_value) > MAX_REVIEW_TERM_LENGTH:
                continue
            candidates.append((term_value, term_type))
            candidate_values.add(term_value)
        row_candidates.append(candidates)

    approved_values = set(
        SearchTerm.objects.annotate(
            normalized_value=Lower('term_value'),
        ).filter(
            normalized_value__in={
                value.casefold() for value in candidate_values
            },
        ).values_list('normalized_value', flat=True)
    )
    row_has_queued_term = []
    row_has_approved_term = []
    row_has_oversized_term = []
    for result, candidates in zip(payload['results'], row_candidates):
        queued = False
        already_approved = False
        oversized = any(
            len(str(item['value']).strip()) > MAX_REVIEW_TERM_LENGTH
            for item in result.get('unmatched_inputs', ())
        )
        for term_value, term_type in candidates:
            if term_value.casefold() in approved_values:
                already_approved = True
                continue
            submission_counts[(term_value, term_type)] += 1
            queued = True
        row_has_queued_term.append(queued)
        row_has_approved_term.append(already_approved)
        row_has_oversized_term.append(oversized)

    if submission_counts:
        values = {term_value for term_value, _term_type in submission_counts}
        existing = {
            (review.term_value, review.term_type): review
            for review in SearchTermReview.objects.filter(term_value__in=values)
        }
        new_reviews = [
            SearchTermReview(
                term_value=term_value,
                term_type=term_type,
                submission_count=count,
                submitted_by=user,
            )
            for (term_value, term_type), count in submission_counts.items()
            if (term_value, term_type) not in existing
        ]
        if new_reviews:
            SearchTermReview.objects.bulk_create(
                new_reviews,
                ignore_conflicts=True,
                batch_size=1000,
            )

        for (term_value, term_type), count in submission_counts.items():
            if (term_value, term_type) in existing:
                SearchTermReview.objects.filter(
                    term_value=term_value,
                    term_type=term_type,
                ).update(
                    submission_count=F('submission_count') + count,
                )

    for result, queued, approved, oversized in zip(
        payload['results'],
        row_has_queued_term,
        row_has_approved_term,
        row_has_oversized_term,
    ):
        result['admin_review_status'] = (
            'Submitted for administrator review'
            if queued
            else (
                'Already approved as a search term'
                if approved
                else (
                    'Some values exceed the 500-character review limit'
                    if oversized
                    else ''
                )
            )
        )

    return sum(row_has_queued_term)


def is_checksum_valid_cas(review):
    return (
        review.term_type.upper() == 'CAS'
        and validate_cas_checksum(review.term_value.strip())
    )


def review_validation_label(review):
    if review.term_type.upper() == 'CAS':
        return (
            'CAS checksum valid'
            if is_checksum_valid_cas(review)
            else 'CAS checksum invalid'
        )
    if review.term_type.upper() in {'SMILES', 'INCHI', 'INCHIKEY'}:
        from apps.registry.services.chemical_lookup import (
            _structure_key_from_representation,
        )

        valid = _structure_key_from_representation(
            review.term_type.upper(),
            review.term_value,
        )
        return 'Structure parses' if valid else 'Structure did not parse'
    return 'Manual review'


def _accepted_term_value(review):
    if review.term_type.upper() == 'CAS':
        validation = detect_and_suggest_cas(review.term_value)
        if validation.get('suggestion') and validate_cas_checksum(
            validation['suggestion']
        ):
            return validation['suggestion']
    return review.term_value.strip()


@transaction.atomic
def accept_search_term_reviews(queryset):
    reviews = list(queryset.select_for_update())
    if not reviews:
        return 0

    term_counts = Counter()
    term_types = {}
    review_ids = []
    for review in reviews:
        value = _accepted_term_value(review)
        term_counts[value] += review.submission_count
        if (
            value not in term_types
            or (
                review.term_type.upper() == 'CAS'
                and term_types[value] != 'CAS'
            )
        ):
            term_types[value] = review.term_type.upper()
        review_ids.append(review.pk)

    existing_terms = {
        term.term_value: term
        for term in SearchTerm.objects.filter(term_value__in=term_counts)
    }
    new_terms = [
        SearchTerm(
            term_value=value,
            term_type=term_types[value],
            frequency_count=count,
        )
        for value, count in term_counts.items()
        if value not in existing_terms
    ]
    if new_terms:
        SearchTerm.objects.bulk_create(
            new_terms,
            ignore_conflicts=True,
            batch_size=1000,
        )
    for value, count in term_counts.items():
        if value in existing_terms:
            SearchTerm.objects.filter(pk=existing_terms[value].pk).update(
                frequency_count=F('frequency_count') + count,
            )

    accepted_terms = SearchTerm.objects.filter(term_value__in=term_counts)
    active_sources = list(WebSource.objects.filter(is_active=True))
    tasks = []
    for term in accepted_terms:
        for source in active_sources:
            provider_code = (source.provider_code or source.name).lower()
            if not _is_valid_source_pairing(term, provider_code):
                continue
            for representation in _representations_for_source(term, provider_code):
                tasks.append(SearchTask(
                    search_term=term,
                    web_source=source,
                    target_representation=representation,
                    status=SearchTask.TaskStatus.PENDING,
                    is_completed=False,
                ))
    if tasks:
        SearchTask.objects.bulk_create(
            tasks,
            ignore_conflicts=True,
            batch_size=1000,
        )

    SearchTermReview.objects.filter(pk__in=review_ids).delete()
    return len(reviews)

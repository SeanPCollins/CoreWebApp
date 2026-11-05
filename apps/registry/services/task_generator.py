from apps.registry.models import SearchTerm, WebSource, SearchTask

# Define which proprietary ID types belong exclusively to which provider
PROPRIETARY_ID_MAPPINGS = {
    'CID': 'pubchem',
    'DTXSID': 'comptox',
    'CHEMBL_ID': 'chembl',
    'CHEMSPIDER': 'chemspider',
    'CSID:': 'chemspider',
}

# Define specific term types that certain providers refuse to process
PROVIDER_EXCLUSIONS = {
    'chembl': ['CAS'],  # CAS-RNs never pair with ChEMBL
}


def _is_masked_identifier_type(term_type):
    normalized = str(term_type or '').strip().upper()
    normalized = normalized.replace('_', ' ').replace('-', ' ')
    return normalized == 'MASKED ID'


def _is_valid_source_pairing(search_term, provider_code):
    """
    Data-driven guard rules with strict exact-match isolation and debugging.
    """
    term_type = (search_term.term_type or '').upper()
    term_value = search_term.term_value.strip().upper()
    provider_code = provider_code.lower().strip()

    if _is_masked_identifier_type(term_type):
        return False

    # 1. Check proprietary ID exclusivity using EXACT provider match
    for prefix, exclusive_provider in PROPRIETARY_ID_MAPPINGS.items():
        if prefix in term_type or term_value.startswith(prefix):
            is_match = (exclusive_provider == provider_code)
            return is_match

    # 2. Check provider-specific exclusions (e.g., ChEMBL blocking CAS)
    excluded_types = PROVIDER_EXCLUSIONS.get(provider_code, [])
    if any(ex in term_type for ex in excluded_types):
        return False

    return True


def _representations_for_source(search_term, provider_code):
    provider_code = provider_code.lower().strip()
    if 'cactus' in provider_code or 'nci' in provider_code:
        if not _is_valid_source_pairing(search_term, provider_code):
            return ()
        return ('stdinchikey', 'smiles', 'iupac_name')
    return ('default',)


def cleanup_invalid_tasks():
    """
    Deletes any existing SearchTasks that violate the provider-identifier rules.
    """
    deleted_count = 0
    for task in SearchTask.objects.select_related('search_term', 'web_source'):
        provider_code = (task.web_source.provider_code or task.web_source.name).lower()
        if not _is_valid_source_pairing(task.search_term, provider_code):
            task.delete()
            deleted_count += 1
    print(f"Cleaned up {deleted_count} invalid cross-provider search tasks.")
    return deleted_count


def propagate_search_tasks_for_term(search_term):
    """
    Takes a single SearchTerm and generates SearchTasks for active WebSources 
    enforcing strict identifier-to-provider mappings.
    """
    active_sources = WebSource.objects.filter(is_active=True)

    for source in active_sources:
        provider_code = (source.provider_code or source.name).lower()

        # Enforce strict pairing guard
        if not _is_valid_source_pairing(search_term, provider_code):
            continue

        # Determine representations
        representations = _representations_for_source(search_term, provider_code)
        for rep in representations:
            SearchTask.objects.get_or_create(
                search_term=search_term,
                web_source=source,
                target_representation=rep,
                defaults={
                    'status': SearchTask.TaskStatus.PENDING,
                    'is_completed': False
                }
            )


def propagate_all_pending_terms():
    """
    Utility to bulk-propagate tasks while strictly guarding provider rules.
    """
    # Optional: Run cleanup first to clear out bad legacy tasks
    cleanup_invalid_tasks()

    terms = SearchTerm.objects.all()
    created_count = 0

    for term in terms:
        active_sources = WebSource.objects.filter(is_active=True)

        for source in active_sources:
            provider_code = (source.provider_code or source.name).lower()

            # Enforce strict pairing guard
            if not _is_valid_source_pairing(term, provider_code):
                continue

            representations = _representations_for_source(term, provider_code)

            for rep in representations:
                _, created = SearchTask.objects.get_or_create(
                    search_term=term,
                    web_source=source,
                    target_representation=rep,
                    defaults={'status': SearchTask.TaskStatus.PENDING, 'is_completed': False}
                )
                if created:
                    created_count += 1

    return created_count

from apps.registry.models import WebSource

WebSource.objects.get_or_create(
    provider_code="chemspider",
    defaults={
        "name": "ChemSpider API",
        "base_url": "https://api.chemspider.com",
        "is_active": True,
        "description": "Royal Society of Chemistry structure and registry resolver (handles CAS-RN, names, identifiers)."
    }
)

from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import WebSource, SearchTerm, SearchTask
from apps.registry.services.task_generator import _is_valid_source_pairing 

@receiver(post_save, sender=WebSource)
def handle_websource_activation(sender, instance, created, **kwargs):
    """
    Automatically triggers when a WebSource is created or updated.
    If the source is active, generates SearchTasks for all existing SearchTerms
    while strictly enforcing identifier-to-provider mappings.
    """
    if instance.is_active:
        source_identifier = (getattr(instance, 'provider_code', None) or instance.name).lower()

        if 'cactus' in source_identifier or 'nci' in source_identifier:
            representations = ['stdinchikey', 'smiles', 'iupac_name']
        else:
            representations = ['default']

        # Bulk-ensure tasks exist for all terms against this active source
        for term in SearchTerm.objects.iterator():
            # ENFORCE GUARD: Skip if this term shouldn't go to this provider
            if not _is_valid_source_pairing(term, source_identifier):
                continue

            for rep in representations:
                SearchTask.objects.get_or_create(
                    search_term=term,
                    web_source=instance,
                    target_representation=rep,
                    defaults={'status': SearchTask.TaskStatus.PENDING}
                )

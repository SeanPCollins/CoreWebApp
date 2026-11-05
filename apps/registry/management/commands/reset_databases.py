from django.core.management.base import BaseCommand
from django.db import transaction
from apps.registry.models import (
    WebFinding, SearchTask, RawSubstanceInput, RawSourceRow, 
    SourceImport, Source, ChemicalActivity, PhysChemProperty, 
    Fingerprint, Identifier, Synonym, StructureClass, Structure, 
    SearchTerm, ChemicalGroup, UnclassifiedPathReview, ProviderTaskLog
)

class Command(BaseCommand):
    help = 'Clears out all ingestion pipeline records, search tasks, and structures while preserving WebSources and Users.'

    def handle(self, *args, **options):
        self.stdout.write("Resetting pipeline and transaction tables...")
        
        with transaction.atomic():
            WebFinding.objects.all().delete()
            SearchTask.objects.all().delete()
            RawSubstanceInput.objects.all().delete()
            RawSourceRow.objects.all().delete()
            SourceImport.objects.all().delete()
            Source.objects.all().delete()
            ChemicalActivity.objects.all().delete()
            PhysChemProperty.objects.all().delete()
            Fingerprint.objects.all().delete()
            Identifier.objects.all().delete()
            Synonym.objects.all().delete()
            StructureClass.objects.all().delete()
            Structure.objects.all().delete()
            SearchTerm.objects.all().delete()
            ChemicalGroup.objects.all().delete()
            UnclassifiedPathReview.objects.all().delete()
            ProviderTaskLog.objects.all().delete()

        self.stdout.write(self.style.SUCCESS("Database reset complete! WebSources and User accounts successfully preserved."))

import json
import uuid
from django.conf import settings
from django.db import models


class UnicodeJSONEncoder(json.JSONEncoder):
    def __init__(self, *args, **kwargs):
        kwargs['ensure_ascii'] = False
        super().__init__(*args, **kwargs)


# ==========================================
# 1. CORE CHEMICAL STRUCTURE & TAXONOMY
# ==========================================

class Structure(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inchikey = models.CharField(max_length=27, unique=True)

    # Standardized values, populated only when supported by evidence or computation.
    inchi = models.TextField(blank=True, null=True)
    canonical_smiles = models.TextField(blank=True, null=True)
    isomeric_smiles = models.TextField(blank=True, null=True)

    molecular_formula = models.CharField(max_length=255, blank=True, null=True)
    molecular_weight = models.FloatField(blank=True, null=True)
    exact_mass = models.FloatField(blank=True, null=True)

    # Provenance for toolkit-generated standardized values.
    standardization_toolkit = models.CharField(max_length=50, blank=True)
    standardization_version = models.CharField(max_length=50, blank=True)
    standardization_policy = models.CharField(max_length=100, blank=True)

    # Keep the existing parent link, but leave it unset until a cleaning
    # policy establishes a defensible parent relationship.
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='derivatives',
        help_text="Parent structure (e.g., neutral free base for a salt form)"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'core_structure'
        verbose_name = 'Chemical Structure'
        verbose_name_plural = 'Chemical Structures'
        indexes = [
            models.Index(fields=['molecular_formula'], name='formula_idx'),
        ]

    def __str__(self):
        return f"{self.molecular_formula} ({self.inchikey})"


class StructureComponent(models.Model):
    structure = models.ForeignKey(
        Structure,
        on_delete=models.CASCADE,
        related_name='component_links',
    )
    component = models.ForeignKey(
        Structure,
        on_delete=models.CASCADE,
        related_name='component_of',
    )
    position = models.PositiveSmallIntegerField()
    count = models.PositiveSmallIntegerField(default=1)
    is_parent_component = models.BooleanField(default=False)

    class Meta:
        db_table = 'core_structure_component'
        constraints = [
            models.UniqueConstraint(
                fields=['structure', 'component'],
                name='unique_structure_component',
            ),
            models.UniqueConstraint(
                fields=['structure'],
                condition=models.Q(is_parent_component=True),
                name='one_parent_component_per_structure',
            ),
        ]
        ordering = ['structure_id', 'position']


class Identifier(models.Model):
    class IdentifierType(models.TextChoices):
        CAS = 'CAS', 'CAS-RN'
        DTXSID = 'DTXSID', 'EPA DTXSID'
        PUBCHEM_CID = 'PUBCHEM_CID', 'PubChem CID'
        CERAPP = 'CERAPP', 'CERAPP ID'
        EINECS = 'EINECS', 'EINECS Number'
        UNII = 'UNII', 'FDA UNII'
        CHEMSPIDER = 'CHEMSPIDER', 'ChemSpider ID'
        MASKED_ID = 'MASKED_ID', 'Masked Identifier'
        OTHER = 'OTHER', 'Other Identifier'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identifier_type = models.CharField(max_length=50, choices=IdentifierType.choices)
    identifier_value = models.CharField(max_length=255, db_index=True)
    structures = models.ManyToManyField(
        Structure,
        through='StructureIdentifier',
        related_name='identifiers',
    )

    class Meta:
        db_table = 'core_identifier'
        verbose_name = 'Chemical Identifier'
        verbose_name_plural = 'Chemical Identifiers'
        unique_together = ('identifier_type', 'identifier_value')

    def __str__(self):
        return f"{self.identifier_type}: {self.identifier_value}"


class StructureIdentifier(models.Model):
    structure = models.ForeignKey(
        Structure,
        on_delete=models.CASCADE,
        related_name='identifier_links',
    )
    identifier = models.ForeignKey(
        Identifier,
        on_delete=models.CASCADE,
        related_name='structure_links',
    )

    class Meta:
        db_table = 'core_structure_identifier'
        constraints = [
            models.UniqueConstraint(
                fields=['structure', 'identifier'],
                name='unique_structure_identifier_link',
            ),
        ]


class Synonym(models.Model):
    class SynonymType(models.TextChoices):
        PREFERRED = 'PREFERRED', 'Preferred Name'
        COMMON = 'COMMON', 'Common Name'
        IUPAC = 'IUPAC', 'IUPAC Name'
        TRADE = 'TRADE', 'Trade Name'
        SYSTEMATIC = 'SYSTEMATIC', 'Systematic Name'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    structure = models.ForeignKey(Structure, on_delete=models.CASCADE, related_name='synonyms')
    synonym_value = models.TextField()
    synonym_type = models.CharField(max_length=50, choices=SynonymType.choices, default=SynonymType.COMMON, db_index=True)
    is_preferred = models.BooleanField(default=False)

    class Meta:
        db_table = 'core_synonym'
        verbose_name = 'Chemical Synonym'
        verbose_name_plural = 'Chemical Synonyms'

    def __str__(self):
        return f"{self.synonym_value} ({self.synonym_type})"


class RefChemicalClass(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    class_name = models.CharField(max_length=255, unique=True)
    source = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        db_table = 'core_ref_chemical_class'
        verbose_name = 'Reference Chemical Class'
        verbose_name_plural = 'Reference Chemical Classes'

    def __str__(self):
        return self.class_name


class StructureClass(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    structure = models.ForeignKey(Structure, on_delete=models.CASCADE, related_name='structure_classes')
    chemical_class = models.ForeignKey(RefChemicalClass, on_delete=models.CASCADE, related_name='assigned_structures')

    class Meta:
        db_table = 'core_structure_class'
        unique_together = ('structure', 'chemical_class')

    def __str__(self):
        return f"{self.structure.molecular_formula} -> {self.chemical_class.class_name}"


class ChemicalGroup(models.Model):
    """Represents a regulatory inventory, user list, or batch set (e.g., 'DSL', 'TSCA Active')."""
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    def __str__(self):
        return self.name


# ==========================================
# 2. PROPERTIES, FINGERPRINTS & TOXICOLOGY
# ==========================================

class PhysChemProperty(models.Model):
    class PropertyType(models.TextChoices):
        LOGP = 'LOGP', 'LogP (Octanol-Water)'
        LOGD = 'LOGD', 'LogD'
        WATER_SOLUBILITY = 'WATER_SOLUBILITY', 'Water Solubility'
        MELTING_POINT = 'MELTING_POINT', 'Melting Point'
        BOILING_POINT = 'BOILING_POINT', 'Boiling Point'
        VAPOR_PRESSURE = 'VAPOR_PRESSURE', 'Vapor Pressure'
        HENRY_LAW = 'HENRY_LAW', 'Henry Law Constant'
        PKA = 'PKA', 'pKa'
        OTHER = 'OTHER', 'Other Property'

    class DeterminationMethod(models.TextChoices):
        EXPERIMENTAL = 'EXP', 'Experimental'
        CALCULATED = 'CALC', 'Calculated / Predicted'
        ESTIMATED = 'EST', 'Estimated'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    structure = models.ForeignKey(Structure, on_delete=models.CASCADE, related_name='physchem_properties')
    property_type = models.CharField(max_length=50, choices=PropertyType.choices, db_index=True)
    numeric_value = models.FloatField()
    unit = models.CharField(max_length=50, blank=True, null=True)
    temperature_celsius = models.FloatField(blank=True, null=True)
    ph = models.FloatField(blank=True, null=True)
    determination_method = models.CharField(max_length=10, choices=DeterminationMethod.choices, default=DeterminationMethod.EXPERIMENTAL, db_index=True)
    source_reference = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'core_physchem_property'
        verbose_name = 'Physicochemical Property'
        verbose_name_plural = 'Physicochemical Properties'


class Fingerprint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    structure = models.OneToOneField(Structure, on_delete=models.CASCADE, related_name='fingerprints')
    pubchem = models.TextField(blank=True, null=True)
    maccs = models.TextField(blank=True, null=True)
    fp2 = models.TextField(blank=True, null=True)
    fp3 = models.TextField(blank=True, null=True)
    fp4 = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_fingerprint'


class RefEndpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'core_ref_endpoint'


class ChemicalActivity(models.Model):
    class DataOrigin(models.TextChoices):
        EXPERIMENTAL = 'EXP', 'Experimental'
        PREDICTED = 'PRED', 'Predicted'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    structure = models.ForeignKey(Structure, on_delete=models.CASCADE, related_name='activities')
    endpoint = models.ForeignKey(RefEndpoint, on_delete=models.CASCADE, related_name='records')
    origin = models.CharField(max_length=10, choices=DataOrigin.choices, default=DataOrigin.EXPERIMENTAL, db_index=True)
    numeric_value = models.FloatField(blank=True, null=True)
    unit = models.CharField(max_length=50, blank=True, null=True)
    qualitative_value = models.CharField(max_length=100, blank=True, null=True)
    source_reference = models.CharField(max_length=255, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'core_chemical_activity'


# ==========================================
# 3. INGESTION, PROVENANCE & SEARCH PIPELINE
# ==========================================

class Source(models.Model):
    """High-level master catalog origin for data batches (e.g., CSV file container)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_name = models.CharField(max_length=255, unique=True, help_text="Master source name / filename")
    original_file_path = models.CharField(max_length=500, blank=True, null=True)
    source_description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_source'
        verbose_name = 'Data Source'
        verbose_name_plural = 'Data Sources'

    def __str__(self):
        return self.source_name


class SourceImport(models.Model):
    """Tracks a specific file upload execution batch, optionally tied to a ChemicalGroup (e.g., DSL)."""
    class ImportStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending Processing'
        QUEUED = 'QUEUED', 'Queued for Processing'
        PROCESSING = 'PROCESSING', 'Processing'
        SUCCESS = 'SUCCESS', 'Completed Successfully'
        FAILED = 'FAILED', 'Failed with Errors'

    class StagingStatus(models.TextChoices):
        NOT_STARTED = 'NOT_STARTED', 'Not started'
        PROCESSING = 'PROCESSING', 'Processing'
        READY = 'READY', 'Ready for review'
        FAILED = 'FAILED', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name='imports')
    
    # CONNECTION TO CHEMICAL GROUP AT THE BATCH/IMPORT LEVEL
    chemical_group = models.ForeignKey(
        ChemicalGroup, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='batch_imports',
        help_text="Optional Chemical Group/List assigned to this entire import batch (e.g., DSL)"
    )

    import_time = models.DateTimeField(auto_now_add=True)
    file_hash = models.CharField(max_length=64, blank=True, null=True, help_text="SHA256 hash of uploaded file")
    file_path = models.CharField(max_length=500, blank=True)
    column_mapping = models.JSONField(default=dict, blank=True)
    file_encoding = models.CharField(max_length=32, default='utf-8-sig')
    row_count = models.IntegerField(default=0)
    import_status = models.CharField(max_length=20, choices=ImportStatus.choices, default=ImportStatus.PENDING)
    progress_message = models.CharField(max_length=255, blank=True)
    error_log = models.TextField(blank=True, null=True)
    original_filename = models.CharField(max_length=255, blank=True)
    headers = models.JSONField(default=list, blank=True)

    staging_status = models.CharField(
        max_length=20,
        choices=StagingStatus.choices,
        default=StagingStatus.NOT_STARTED,
        db_index=True,
    )
    staged_row_count = models.PositiveBigIntegerField(default=0)
    staging_error = models.TextField(blank=True)

    class Meta:
        db_table = 'core_source_import'
        constraints = [
            models.CheckConstraint(
                check=models.Q(row_count__gte=0),
                name='import_row_count_nonneg',
            ),
        ]
        verbose_name = 'Source Import Batch'
        verbose_name_plural = 'Source Import Batches'

    def __str__(self):
        return f"Import Batch {self.id} for {self.source.source_name} ({self.import_status})"


class StagedImportRow(models.Model):
    source_import = models.ForeignKey(
        SourceImport,
        on_delete=models.CASCADE,
        related_name='staged_rows',
    )
    row_index = models.PositiveBigIntegerField()
    sort_priority = models.PositiveSmallIntegerField(default=1)
    staged_data = models.TextField()
    is_valid = models.BooleanField(default=False)
    is_existing = models.BooleanField(default=False)

    class Meta:
        db_table = 'core_staged_import_row'
        constraints = [
            models.UniqueConstraint(
                fields=('source_import', 'row_index'),
                name='unique_staged_import_row',
            ),
        ]
        indexes = [
            models.Index(
                fields=('source_import', 'sort_priority', 'row_index'),
                name='staged_import_page_idx',
            ),
        ]
        ordering = ('sort_priority', 'row_index')


class RawSourceRow(models.Model):
    """Represents a single physical row in a specific SourceImport batch."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_import = models.ForeignKey(SourceImport, on_delete=models.CASCADE, related_name='raw_rows')
    row_index = models.IntegerField(help_text="Line number in original file")
    original_json_dump = models.TextField(help_text="JSON representation of raw CSV row dictionary")

    class Meta:
        db_table = 'core_raw_source_row'
        constraints = [
            models.UniqueConstraint(
                fields=['source_import', 'row_index'],
                name='unique_row_per_source_import',
            ),
            models.CheckConstraint(
                check=models.Q(row_index__gte=0),
                name='raw_source_row_index_nonnegative',
            ),
        ]
        verbose_name = 'Raw Source Row'
        verbose_name_plural = 'Raw Source Rows'

    def __str__(self):
        return f"Row #{self.row_index} (Batch {self.source_import_id})"


class SearchTerm(models.Model):
    """Centralized dictionary of unique, normalized search strings and terms."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    term_value = models.TextField(unique=True)
    term_type = models.CharField(max_length=50, blank=True, null=True)

    resolved_structure = models.ForeignKey(
        Structure,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='search_terms'
    )

    frequency_count = models.IntegerField(default=1)
    groups = models.ManyToManyField(ChemicalGroup, related_name='search_terms', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'core_search_term'
        verbose_name = 'Search Term'
        verbose_name_plural = 'Search Terms'

    def __str__(self):
        return self.term_value


class SearchTermReview(models.Model):
    term_value = models.TextField()
    term_type = models.CharField(max_length=50)
    submission_count = models.PositiveIntegerField(default=1)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='search_term_reviews',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'core_search_term_review'
        constraints = [
            models.UniqueConstraint(
                fields=('term_value', 'term_type'),
                name='unique_search_term_review',
            ),
        ]
        ordering = ['created_at', 'pk']
        verbose_name = 'Search term review'
        verbose_name_plural = 'Search term review queue'

    def __str__(self):
        return f'{self.term_type}: {self.term_value}'


class RawSubstanceInput(models.Model):
    """Extracted value from a source row, linked to a term or identifier."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_row = models.ForeignKey(RawSourceRow, on_delete=models.CASCADE, related_name='substance_inputs')
    
    # DIRECT CONNECTION TO SEARCH TERM
    search_term = models.ForeignKey(
        SearchTerm,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='raw_inputs',
        help_text="The canonical SearchTerm resulting from this input"
    )
    identifier = models.ForeignKey(
        Identifier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='raw_inputs',
        help_text=(
            'Catalog identifier recorded from this source input; the raw row '
            'links it to accompanying names and other values.'
        ),
    )

    input_type = models.CharField(max_length=50)
    input_value = models.TextField()
    is_processed = models.BooleanField(default=False)

    class Meta:
        db_table = 'core_raw_substance_input'
        verbose_name = 'Raw Substance Input'
        verbose_name_plural = 'Raw Substance Inputs'

    def __str__(self):
        return f"{self.input_type}: {self.input_value}"


class WebSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    provider_code = models.CharField(max_length=50, unique=True)
    base_url = models.URLField(max_length=500, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_web_source'
        verbose_name = 'Web Source / API Target'
        verbose_name_plural = 'Web Sources & API Targets'

    def __str__(self):
        return self.name


class SearchTask(models.Model):
    class TaskStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PROCESSING = 'PROCESSING', 'Processing'
        SUCCESS_FOUND = 'SUCCESS_FOUND', 'Successful'
        NOT_FOUND = 'NOT_FOUND', 'Confirmed Not Found (404)'
        ERROR = 'ERROR', 'Transient Error'
        SKIPPED = 'SKIPPED', 'Skipped'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    search_term = models.ForeignKey(SearchTerm, on_delete=models.CASCADE, related_name='tasks')
    web_source = models.ForeignKey(WebSource, on_delete=models.CASCADE, related_name='tasks')
    target_representation = models.CharField(max_length=50, default='default')

    status = models.CharField(max_length=50, choices=TaskStatus.choices, default=TaskStatus.PENDING, db_index=True)
    is_completed = models.BooleanField(default=False, db_index=True)
    last_run_at = models.DateTimeField(blank=True, null=True)
    run_count = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'core_search_task'
        constraints = [
            models.CheckConstraint(
                check=models.Q(run_count__gte=0),
                name='search_run_count_nonneg',
            ),
        ]
        verbose_name = 'Search Task'
        verbose_name_plural = 'Search Tasks'
        unique_together = ('search_term', 'web_source', 'target_representation')

    def __str__(self):
        return str(self.search_term.term_value if self.search_term else self.id)

class StructureEvidence(models.Model):
    class Assessment(models.TextChoices):
        SUPPORTS = 'SUPPORTS', 'Supports structure'
        CONFLICTS = 'CONFLICTS', 'Conflicts with structure'
        INDETERMINATE = 'INDETERMINATE', 'Cannot determine'

    structure = models.ForeignKey(
        Structure,
        on_delete=models.CASCADE,
        related_name='evidence',
    )
    web_finding = models.ForeignKey(
        'WebFinding',
        on_delete=models.CASCADE,
        related_name='structure_evidence',
    )

    assessment = models.CharField(
        max_length=20,
        choices=Assessment.choices,
    )
    # Extracted claims and validation outcomes for review. The original source
    # payload remains available on WebFinding.
    reported_values = models.JSONField(
        default=dict,
        blank=True,
        encoder=UnicodeJSONEncoder,
    )
    validation = models.JSONField(
        default=dict,
        blank=True,
        encoder=UnicodeJSONEncoder,
    )

    class Meta:
        db_table = 'core_structure_evidence'
        constraints = [
            models.UniqueConstraint(
                fields=['structure', 'web_finding'],
                name='unique_structure_web_finding_evidence',
            ),
        ]
        indexes = [
            models.Index(
                fields=['web_finding', 'assessment'],
                name='str_ev_fnd_assess_idx',
            ),
        ]

class WebFinding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    search_task = models.ForeignKey(SearchTask, on_delete=models.CASCADE, related_name='findings')
    source_url = models.TextField(blank=True, null=True)
    raw_payload = models.TextField()
    parsed_successfully = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'core_web_finding'
        verbose_name = 'Web Finding'
        verbose_name_plural = 'Web Findings'
        indexes = [
            models.Index(
                fields=['updated_at', 'id'],
                name='webfinding_updated_pk_idx',
            ),
        ]


class StructureCurationQueueCursor(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    created_at = models.DateTimeField(null=True, blank=True)
    finding_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = 'core_structure_curation_queue_cursor'


class StructureCurationBatch(models.Model):
    class Status(models.TextChoices):
        SCANNING = 'SCANNING', 'Scanning'
        NEEDS_REVIEW = 'NEEDS_REVIEW', 'Needs review'
        READY = 'READY', 'Ready to commit'
        COMMITTED = 'COMMITTED', 'Committed'
        FAILED = 'FAILED', 'Failed'
        SUPERSEDED = 'SUPERSEDED', 'Reprocessed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run_id = models.UUIDField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    batch_limit = models.PositiveIntegerField()
    random_seed = models.PositiveBigIntegerField()
    candidate_count = models.PositiveIntegerField(default=0)
    selected_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SCANNING,
    )
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'core_structure_curation_batch'
        ordering = ['-created_at']


class StructureCurationBatchFinding(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SELECTED = 'SELECTED', 'Selected'
        NEEDS_WORK = 'NEEDS_WORK', 'Needs work'
        ACCEPTED = 'ACCEPTED', 'Accepted'
        REJECTED = 'REJECTED', 'Rejected'

    batch = models.ForeignKey(
        StructureCurationBatch,
        on_delete=models.CASCADE,
        related_name='findings',
        null=True,
        blank=True,
    )
    finding = models.OneToOneField(
        WebFinding,
        on_delete=models.CASCADE,
        related_name='curation_queue_item',
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    class Meta:
        db_table = 'core_structure_curation_batch_finding'


class UnclassifiedPathReview(models.Model):
    path = models.CharField(max_length=500, unique=True)
    sample_data = models.JSONField()
    first_seen_at = models.DateTimeField(auto_now_add=True)
    occurrence_count = models.PositiveIntegerField(default=1)


class ProviderTaskLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=50, default='cactus')
    status = models.CharField(max_length=50, default='RUNNING')
    total_items = models.IntegerField(default=0)
    processed_items = models.IntegerField(default=0)
    success_count = models.IntegerField(default=0)
    not_found_count = models.IntegerField(default=0)
    error_count = models.IntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    log_summary = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'core_provider_task_log'
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(total_items__gte=0)
                    & models.Q(processed_items__gte=0)
                    & models.Q(success_count__gte=0)
                    & models.Q(not_found_count__gte=0)
                    & models.Q(error_count__gte=0)
                ),
                name='provider_counts_nonneg',
            ),
        ]
        ordering = ['-started_at']

class DataGatheringRun(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = 'SCHEDULED', 'Scheduled'
        RUNNING = 'RUNNING', 'Running'
        PAUSED = 'PAUSED', 'Paused'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider_code = models.CharField(max_length=50, db_index=True)
    scheduled_for = models.DateTimeField(db_index=True)
    max_runtime_minutes = models.PositiveIntegerField(default=360)
    batch_size = models.PositiveIntegerField(default=500)
    dispatched_at = models.DateTimeField(null=True, blank=True, db_index=True)
    max_tasks = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text='Maximum SearchTask rows to process; blank means no task limit.',
    )
    force_chemspider_small_batches = models.BooleanField(
        default=False,
        help_text='Allow ChemSpider detail requests with fewer than 50 IDs.',
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SCHEDULED,
        db_index=True,
    )
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    processed_count = models.PositiveIntegerField(default=0)
    pause_reason = models.TextField(blank=True, null=True)
    rate_limit_retries = models.PositiveIntegerField(default=0)
    is_dismissed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'core_data_gathering_run'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.provider_code} data-gathering run ({self.status})"
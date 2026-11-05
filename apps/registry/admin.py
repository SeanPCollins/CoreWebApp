from django.contrib import admin
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.tokens import default_token_generator
from django.conf import settings
from django.core.cache import cache
import json
import os
from django.utils import timezone
from django.db.models import Count
from django import forms
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect, render
import tempfile
from django.utils.html import format_html, format_html_join
from .models import (
    Structure,
    Identifier,
    StructureIdentifier,
    Synonym,
    RefChemicalClass,
    StructureClass,
    PhysChemProperty,
    Fingerprint,
    RefEndpoint,
    ChemicalActivity,
    Source,
    SourceImport,
    RawSourceRow,
    RawSubstanceInput,
    WebSource,
    SearchTerm,
    SearchTermReview,
    SearchTask,
    ProviderTaskLog,
    WebFinding,
    ChemicalGroup,
    UnclassifiedPathReview,
    DataGatheringRun,
)
from .forms import UsernamePasswordResetForm
from .services.search_term_review import (
    accept_search_term_reviews,
    is_checksum_valid_cas,
    review_validation_label,
)


User = get_user_model()


class AccountUserAdmin(DjangoUserAdmin):
    actions = DjangoUserAdmin.actions + ('send_password_reset_links',)

    def get_urls(self):
        custom_urls = [
            path(
                '<path:object_id>/send-password-reset/',
                self.admin_site.admin_view(self.send_password_reset_view),
                name='auth_user_send_password_reset',
            ),
        ]
        return custom_urls + super().get_urls()

    def _send_password_reset_email(self, request, user):
        if not user.is_active or not user.has_usable_password() or not user.email:
            return False
        reset_form = UsernamePasswordResetForm(data={'email': user.username})
        if not reset_form.is_valid():
            raise ValueError(
                f'Could not prepare password reset email for {user.username}: '
                f'{reset_form.errors.as_text()}'
            )
        reset_form.save(
            use_https=request.is_secure(),
            token_generator=default_token_generator,
            from_email=settings.DEFAULT_FROM_EMAIL,
            request=request,
            domain_override=request.get_host(),
            email_template_name='core/password_reset_email.html',
            subject_template_name='core/password_reset_subject.txt',
        )
        return True

    def send_password_reset_view(self, request, object_id):
        user = get_object_or_404(self.get_queryset(request), pk=object_id)
        change_url = reverse('admin:auth_user_change', args=(user.pk,))
        eligible = (
            user.is_active
            and user.has_usable_password()
            and bool(user.email)
        )
        if request.method == 'POST':
            if not eligible:
                messages.warning(
                    request,
                    'A reset link cannot be sent because this account is inactive, '
                    'has no usable password, or has no email address.',
                )
            elif self._send_password_reset_email(request, user):
                messages.success(
                    request,
                    f'A password reset link was sent to {user.email}.',
                )
            return redirect(change_url)

        return render(
            request,
            'admin/auth/user/send_password_reset.html',
            {
                **self.admin_site.each_context(request),
                'opts': self.model._meta,
                'title': 'Send password reset link',
                'user_account': user,
                'eligible': eligible,
                'change_url': change_url,
            },
        )

    @admin.action(description='Send password reset link to selected users')
    def send_password_reset_links(self, request, queryset):
        sent = 0
        skipped = 0
        for user in queryset:
            if not self._send_password_reset_email(request, user):
                skipped += 1
                continue
            sent += 1

        if sent:
            self.message_user(
                request,
                f'Password reset email sent to {sent} user(s).',
                level=messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                f'{skipped} selected user(s) were skipped because their account is inactive, has no usable password, or has no email address.',
                level=messages.WARNING,
            )


admin.site.unregister(User)
admin.site.register(User, AccountUserAdmin)

# =====================================================================
# 1. CUSTOM FORMS & WIDGETS (Optimized for 100k+ Scale)
# =====================================================================
class IdentifierDatalistInput(forms.TextInput):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.options = ()

    def render(self, name, value, attrs=None, renderer=None):
        final_attrs = self.build_attrs(self.attrs, attrs)
        list_id = f"{final_attrs.get('id', name)}_options"
        final_attrs['list'] = list_id

        input_html = super().render(name, value, final_attrs, renderer)
        options_html = format_html_join(
            "",
            '<option value="{}"></option>',
            ((option,) for option in self.options),
        )
        return format_html(
            '{}<datalist id="{}">{}</datalist>',
            input_html,
            list_id,
            options_html,
        )


class IdentifierAdminForm(forms.ModelForm):
    class Meta:
        model = Identifier
        fields = '__all__'
        widgets = {
            'identifier_value': IdentifierDatalistInput(attrs={
                'autocomplete': 'off',
                'placeholder': 'Type to search...',
            })
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        cache_key = 'admin_identifier_options'
        options = cache.get(cache_key)
        if options is None:
            options = list(
                Identifier.objects
                .exclude(identifier_value__isnull=True)
                .exclude(identifier_value='')
                .order_by('identifier_value')
                .distinct()
                .values_list('identifier_value', flat=True)[:500]
            )
            cache.set(cache_key, options, timeout=3600)

        self.fields['identifier_value'].widget.options = options
        self.fields['identifier_value'].help_text = (
            'Type or select an existing identifier.'
        )


# =====================================================================
# 2. INLINE CONFIGURATIONS
# =====================================================================

class StructureIdentifierInline(admin.TabularInline):
    model = StructureIdentifier
    raw_id_fields = ('identifier',)
    extra = 1

class SynonymInline(admin.TabularInline):
    model = Synonym
    extra = 1

class PhysChemPropertyInline(admin.TabularInline):
    model = PhysChemProperty
    extra = 1

class StructureClassInline(admin.TabularInline):
    model = StructureClass
    extra = 1

class ChemicalActivityInline(admin.TabularInline):
    model = ChemicalActivity
    extra = 1

class FingerprintInline(admin.StackedInline):
    model = Fingerprint
    can_delete = False


# --- Task & Finding Inlines ---
class WebFindingInline(admin.TabularInline):
    model = WebFinding
    extra = 0
    readonly_fields = ['finding_link', 'source_url', 'truncated_payload', 'parsed_successfully', 'created_at']
    fields = ('finding_link', 'source_url', 'truncated_payload', 'parsed_successfully', 'created_at')

    def truncated_payload(self, obj):
        if not obj.raw_payload:
            return "-"
        return obj.raw_payload[:100] + "..." if len(obj.raw_payload) > 100 else obj.raw_payload
    truncated_payload.short_description = 'Raw Payload'

    def finding_link(self, obj):
        if not obj.pk:
            return "-"
        url = reverse('admin:registry_webfinding_change', args=[obj.pk])
        return format_html('<a href="{}">View Finding</a>', url)
    finding_link.short_description = 'Action'


# --- Ingestion & Audit Pipeline Inlines ---
class RawSubstanceInputInline(admin.TabularInline):
    model = RawSubstanceInput
    extra = 0
    readonly_fields = (
        'input_link',
        'input_type',
        'input_value',
        'search_term_link',
        'identifier',
        'is_processed',
    )
    fields = (
        'input_link',
        'input_type',
        'input_value',
        'search_term_link',
        'identifier',
        'is_processed',
    )
    can_delete = False

    def input_link(self, obj):
        if not obj.pk:
            return "-"
        url = reverse('admin:registry_rawsubstanceinput_change', args=[obj.pk])
        return format_html('<a href="{}"><b>View Input Record</b></a>', url)
    input_link.short_description = 'Input Record Link'

    def search_term_link(self, obj):
        if not obj.search_term:
            return "Not linked"
        url = reverse('admin:registry_searchterm_change', args=[obj.search_term.pk])
        return format_html('<a href="{}"><b>{}</b></a>', url, obj.search_term.term_value)
    search_term_link.short_description = 'Canonical Search Term'


class RawSourceRowInline(admin.TabularInline):
    model = RawSourceRow
    extra = 0
    readonly_fields = ('row_link', 'row_index', 'truncated_json_dump', 'inputs_summary')
    fields = ('row_link', 'row_index', 'truncated_json_dump', 'inputs_summary')
    can_delete = False

    def row_link(self, obj):
        if not obj.pk:
            return "-"
        url = reverse('admin:registry_rawsourcerow_change', args=[obj.pk])
        return format_html('<a href="{}">View Row #{}</a>', url, obj.row_index)
    row_link.short_description = 'Raw Row Detail'

    def truncated_json_dump(self, obj):
        if not obj.original_json_dump:
            return "-"
        return obj.original_json_dump[:80] + "..." if len(obj.original_json_dump) > 80 else obj.original_json_dump
    truncated_json_dump.short_description = 'Raw Row Data'

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('substance_inputs')

    def inputs_summary(self, obj):
        inputs = list(obj.substance_inputs.all())
        if not inputs:
            return "No extracted inputs"
        return ", ".join(
            f"{item.input_type}: {item.input_value}"
            for item in inputs
        )

    inputs_summary.short_description = 'Extracted Inputs'


class SourceImportInline(admin.TabularInline):
    model = SourceImport
    extra = 0
    readonly_fields = (
        'import_link',
        'import_time',
        'chemical_group',
        'file_hash',
        'batch_row_count',
        'progress_message',
        'import_status',
    )
    fields = (
        'import_link',
        'import_time',
        'chemical_group',
        'file_hash',
        'batch_row_count',
        'progress_message',
        'import_status',
    )

    def import_link(self, obj):
        if not obj.pk:
            return "-"
        url = reverse('admin:registry_sourceimport_change', args=[obj.pk])
        return format_html('<a href="{}">Batch #{}</a>', url, str(obj.pk)[:8])
    import_link.short_description = 'Import Batch'

    def batch_row_count(self, obj):
        if obj.import_status == SourceImport.ImportStatus.SUCCESS:
            return obj.row_count
        return obj.staged_row_count

    batch_row_count.short_description = 'Rows staged / imported'


class SearchTermInline(admin.TabularInline):
    model = SearchTerm.groups.through
    extra = 0
    verbose_name = "Tagged Chemical"
    verbose_name_plural = "Tagged Chemicals in this Group"


class SearchTaskInline(admin.TabularInline):
    model = SearchTask
    extra = 0
    readonly_fields = ['task_link', 'web_source', 'status', 'is_completed', 'run_count', 'last_run_at']
    fields = ('task_link', 'web_source', 'status', 'is_completed', 'run_count', 'last_run_at')

    def task_link(self, obj):
        if not obj.pk:
            return "-"
        url = reverse('admin:registry_searchtask_change', args=[obj.pk])
        return format_html('<a href="{}">View Task</a>', url)
    task_link.short_description = 'Action'


# =====================================================================
# 3. CORE CHEMICAL STRUCTURE & TAXONOMY ADMIN
# =====================================================================

@admin.register(Structure)
class StructureAdmin(admin.ModelAdmin):
    list_display = ('inchikey', 'molecular_formula', 'molecular_weight', 'created_at')
    list_display_links = ('inchikey',)
    search_fields = ('inchikey', 'molecular_formula', 'canonical_smiles')
    list_filter = ('created_at',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    raw_id_fields = ('parent',)

    inlines = [
        StructureIdentifierInline,
        SynonymInline,
        PhysChemPropertyInline,
        StructureClassInline,
        ChemicalActivityInline,
        FingerprintInline,
    ]

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        if formset.model is StructureIdentifier:
            cache.delete('admin_identifier_options')


@admin.register(Identifier)
class IdentifierAdmin(admin.ModelAdmin):
    list_display = ('identifier_value', 'identifier_type', 'linked_structures')
    list_display_links = ('identifier_value',)
    fields = (
        'identifier_type',
        'identifier_value',
        'linked_structures',
    )
    readonly_fields = ('linked_structures',)
    search_fields = (
        'identifier_value',
        'structures__inchikey',
        'structures__molecular_formula',
    )
    list_filter = ('identifier_type',)
    raw_id_fields = ('structures',)

    def linked_structures(self, obj):
        return format_html_join(
            ', ',
            '<a href="{}">{}</a>',
            (
                (
                    reverse(
                        'admin:registry_structure_change',
                        args=[structure.pk],
                    ),
                    structure.inchikey,
                )
                for structure in obj.structures.all().order_by('inchikey')
            ),
        )

    linked_structures.short_description = 'Structures'

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('structures')

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        cache.delete('admin_identifier_options')

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        cache.delete('admin_identifier_options')

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)
        cache.delete('admin_identifier_options')

@admin.register(Synonym)
class SynonymAdmin(admin.ModelAdmin):
    list_display = ('synonym_value', 'synonym_type', 'is_preferred', 'structure')
    search_fields = ('synonym_value', 'structure__inchikey')
    list_filter = ('synonym_type', 'is_preferred')


@admin.register(RefChemicalClass)
class RefChemicalClassAdmin(admin.ModelAdmin):
    list_display = ('class_name', 'source')
    search_fields = ('class_name', 'source')


@admin.register(StructureClass)
class StructureClassAdmin(admin.ModelAdmin):
    list_display = ('structure', 'chemical_class')
    search_fields = ('structure__inchikey', 'chemical_class__class_name')
    list_filter = ('chemical_class',)


@admin.register(ChemicalGroup)
class ChemicalGroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'created_at', 'term_count')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at',)
    inlines = [SearchTermInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            admin_term_count=Count('search_terms', distinct=True)
        )

    def term_count(self, obj):
        return obj.admin_term_count

    term_count.short_description = 'Tagged Chemicals'


# =====================================================================
# 4. PROPERTIES, FINGERPRINTS & TOXICOLOGY ADMIN
# =====================================================================

@admin.register(PhysChemProperty)
class PhysChemPropertyAdmin(admin.ModelAdmin):
    list_display = ('property_type', 'numeric_value', 'unit', 'determination_method', 'structure', 'temperature_celsius')
    search_fields = ('property_type', 'source_reference', 'structure__inchikey', 'structure__molecular_formula')
    list_filter = ('property_type', 'determination_method', 'unit')


@admin.register(Fingerprint)
class FingerprintAdmin(admin.ModelAdmin):
    list_display = ('structure', 'created_at')
    search_fields = ('structure__inchikey', 'structure__molecular_formula')


@admin.register(RefEndpoint)
class RefEndpointAdmin(admin.ModelAdmin):
    list_display = ('code', 'name')
    search_fields = ('code', 'name', 'description')


@admin.register(ChemicalActivity)
class ChemicalActivityAdmin(admin.ModelAdmin):
    list_display = ('structure', 'endpoint', 'origin', 'numeric_value', 'unit', 'qualitative_value', 'source_reference')
    search_fields = ('endpoint__code', 'endpoint__name', 'source_reference', 'structure__inchikey', 'structure__molecular_formula')
    list_filter = ('origin', 'endpoint')


# =====================================================================
# 5. INGESTION, PROVENANCE & SEARCH PIPELINE ADMIN
# =====================================================================

@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ('source_name', 'is_active', 'created_at', 'import_count')
    search_fields = ('source_name', 'source_description')
    list_filter = ('is_active',)
    readonly_fields = ('created_at',)
    inlines = [SourceImportInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            admin_import_count=Count('imports')
        )

    def import_count(self, obj):
        return obj.admin_import_count

    import_count.short_description = 'Total Batches Imported'


@admin.register(SourceImport)
class SourceImportAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'source_link',
        'chemical_group',
        'import_status',
        'batch_row_count',
        'progress_message',
        'import_time',
    )
    search_fields = ('source__source_name', 'file_hash', 'chemical_group__name')
    list_filter = ('import_status', 'source', 'chemical_group')
    readonly_fields = (
        'import_time',
        'file_hash',
        'file_path',
        'source_link',
        'column_mapping',
        'file_encoding',
        'staging_status',
        'staged_row_count',
        'progress_message',
        'error_log',
    )
    inlines = [RawSourceRowInline]

    def batch_row_count(self, obj):
        if obj.import_status == SourceImport.ImportStatus.SUCCESS:
            return obj.row_count
        return obj.staged_row_count

    batch_row_count.short_description = 'Rows staged / imported'

    def source_link(self, obj):
        if not obj.source:
            return "-"
        url = reverse('admin:registry_source_change', args=[obj.source.pk])
        return format_html('<a href="{}">{}</a>', url, obj.source.source_name)
    source_link.short_description = 'Master Data Source'


@admin.register(RawSourceRow)
class RawSourceRowAdmin(admin.ModelAdmin):
    list_display = ('id', 'import_batch_link', 'row_index')
    search_fields = ('source_import__source__source_name', 'original_json_dump')
    list_filter = ('source_import',)
    readonly_fields = ('original_json_dump', 'import_batch_link')
    inlines = [RawSubstanceInputInline]

    def import_batch_link(self, obj):
        if not obj.source_import:
            return "-"
        url = reverse('admin:registry_sourceimport_change', args=[obj.source_import.pk])
        source_name = obj.source_import.source.source_name if obj.source_import.source else "Source"
        return format_html('<a href="{}">{} (Batch Import #{})</a>', url, source_name, str(obj.source_import.pk)[:8])
    import_batch_link.short_description = 'Parent Source Import Batch'


@admin.register(RawSubstanceInput)
class RawSubstanceInputAdmin(admin.ModelAdmin):
    list_display = (
        'input_value',
        'input_type',
        'parent_row_link',
        'import_batch_link',
        'search_term_link',
        'identifier',
        'is_processed',
    )
    search_fields = ('input_value', 'input_type')
    list_filter = ('input_type', 'is_processed')
    readonly_fields = ('parent_row_link', 'import_batch_link', 'search_term_link', 'is_processed')
    fields = (
        'search_term_link',
        'identifier',
        'input_type',
        'input_value',
        'parent_row_link',
        'import_batch_link',
        'is_processed',
    )

    def parent_row_link(self, obj):
        if not obj.raw_row:
            return "-"
        url = reverse('admin:registry_rawsourcerow_change', args=[obj.raw_row.pk])
        return format_html('<a href="{}"><b>View Raw Row #{}</b></a>', url, obj.raw_row.row_index)
    parent_row_link.short_description = 'Parent Raw Row'

    def import_batch_link(self, obj):
        if not obj.raw_row or not obj.raw_row.source_import:
            return "-"
        imp = obj.raw_row.source_import
        url = reverse('admin:registry_sourceimport_change', args=[imp.pk])
        source_name = imp.source.source_name if imp.source else "Source"
        return format_html('<a href="{}">{} (Batch Import)</a>', url, source_name)
    import_batch_link.short_description = 'Source Import Batch'

    def search_term_link(self, obj):
        if not obj.search_term:
            return "Not linked"
        url = reverse('admin:registry_searchterm_change', args=[obj.search_term.pk])
        return format_html('<a href="{}"><b>{}</b></a>', url, obj.search_term.term_value)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'raw_row__source_import__source',
            'search_term',
            'identifier',
        )

    search_term_link.short_description = 'Canonical Search Term'


@admin.register(WebSource)
class WebSourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    search_fields = ('name', 'provider_code', 'description')
    list_filter = ('is_active',)


@admin.register(SearchTerm)
class SearchTermAdmin(admin.ModelAdmin):
    list_display = ('term_value', 'term_type', 'frequency_count', 'group_list', 'resolved_structure', 'created_at')
    search_fields = ('term_value', 'term_type', 'resolved_structure__inchikey')
    list_filter = ('term_type', 'groups')
    filter_horizontal = ('groups',)

    readonly_fields = ('frequency_count', 'created_at', 'updated_at', 'task_links_list')
    inlines = [SearchTaskInline, RawSubstanceInputInline]

    fieldsets = (
        (None, {
            'fields': ('term_value', 'term_type', 'resolved_structure', 'frequency_count', 'task_links_list', 'groups', 'created_at', 'updated_at')
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related(
            'groups',
            'tasks__web_source',
        )

    def task_links_list(self, obj):
        tasks = list(obj.tasks.all())
        if not tasks:
            return "No tasks linked"

        return format_html_join(
            ", ",
            '<a href="{}">{} ({})</a>',
            (
                (
                    reverse('admin:registry_searchtask_change', args=[task.pk]),
                    task.web_source.name if task.web_source else f"Task {task.pk}",
                    task.status,
                )
                for task in tasks
            ),
        )

    def group_list(self, obj):
        return ", ".join(group.name for group in obj.groups.all())

    group_list.short_description = 'Assigned Groups'


@admin.register(SearchTermReview)
class SearchTermReviewAdmin(admin.ModelAdmin):
    list_display = (
        'term_value',
        'term_type',
        'validation_summary',
        'submission_count',
        'submitted_by',
        'created_at',
    )
    list_filter = ('term_type',)
    search_fields = ('term_value', 'term_type', 'submitted_by__username')
    readonly_fields = ('submission_count', 'submitted_by', 'created_at', 'updated_at')
    actions = (
        'accept_selected',
        'accept_checksum_valid_cas',
        'reject_selected',
    )

    @admin.display(description='Automated check')
    def validation_summary(self, obj):
        return review_validation_label(obj)

    @admin.action(description='Accept selected and create pending provider tasks')
    def accept_selected(self, request, queryset):
        accepted = accept_search_term_reviews(queryset)
        self.message_user(
            request,
            f'Accepted {accepted} review item(s). Provider tasks were created '
            'as pending and have not been run.',
            messages.SUCCESS,
        )

    @admin.action(description='Accept selected checksum-valid CAS numbers')
    def accept_checksum_valid_cas(self, request, queryset):
        eligible = [
            review.pk
            for review in queryset.iterator()
            if is_checksum_valid_cas(review)
        ]
        accepted = accept_search_term_reviews(
            SearchTermReview.objects.filter(pk__in=eligible)
        )
        self.message_user(
            request,
            f'Accepted {accepted} checksum-valid CAS review item(s). '
            'Provider tasks were created as pending and have not been run.',
            messages.SUCCESS,
        )

    @admin.action(description='Reject selected and permanently delete them')
    def reject_selected(self, request, queryset):
        rejected, _details = queryset.delete()
        self.message_user(
            request,
            f'Permanently deleted {rejected} selected review item(s).',
            messages.SUCCESS,
        )


@admin.register(SearchTask)
class SearchTaskAdmin(admin.ModelAdmin):
    list_display = ('get_term_value', 'web_source', 'status', 'is_completed', 'run_count', 'last_run_at')
    search_fields = ('search_term__term_value', 'web_source__name', 'web_source__provider_code')
    list_filter = ('status', 'is_completed', 'web_source', 'target_representation')
    readonly_fields = ('parent_term_link', 'last_run_at', 'run_count')
    autocomplete_fields = ('search_term',)
    inlines = [WebFindingInline]

    fieldsets = (
        (None, {
            'fields': ('parent_term_link', 'search_term', 'web_source', 'target_representation', 'status', 'is_completed', 'run_count', 'last_run_at')
        }),
    )

    def get_term_value(self, obj):
        return obj.search_term.term_value if obj.search_term else "-"
    get_term_value.short_description = 'Search Term'
    get_term_value.admin_order_field = 'search_term__term_value'

    def parent_term_link(self, obj):
        if not obj.search_term:
            return "No parent search term"
        url = reverse('admin:registry_searchterm_change', args=[obj.search_term.pk])
        return format_html('<a href="{}">View Parent Search Term: {}</a>', url, obj.search_term.term_value)
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'search_term',
            'web_source',
        )

    parent_term_link.short_description = 'Parent Search Term'


@admin.register(WebFinding)
class WebFindingAdmin(admin.ModelAdmin):
    list_display = ('get_task_link', 'source_url', 'parsed_successfully', 'created_at', 'updated_at')
    search_fields = ('source_url', 'raw_payload', 'search_task__search_term__term_value')
    list_filter = ('parsed_successfully', 'created_at', 'updated_at')
    readonly_fields = ('parent_task_link', 'search_task', 'source_url', 'raw_payload', 'parsed_successfully', 'created_at', 'updated_at')

    fieldsets = (
        (None, {
            'fields': ('parent_task_link', 'search_task', 'source_url', 'raw_payload', 'parsed_successfully', 'created_at', 'updated_at')
        }),
    )

    def get_task_link(self, obj):
        if not obj.search_task:
            return "-"
        url = reverse('admin:registry_searchtask_change', args=[obj.search_task.pk])
        return format_html('<a href="{}">View Task</a>', url)
    get_task_link.short_description = 'Parent Search Task'

    def parent_task_link(self, obj):
        if not obj.search_task:
            return "No parent search task"
        url = reverse('admin:registry_searchtask_change', args=[obj.search_task.pk])
        term_val = obj.search_task.search_term.term_value if obj.search_task.search_term else f"Task {obj.search_task.pk}"
        return format_html('<a href="{}">View Parent Search Task ({})</a>', url, term_val)
    parent_task_link.short_description = 'Parent Search Task'


@admin.register(UnclassifiedPathReview)
class UnclassifiedPathReviewAdmin(admin.ModelAdmin):
    list_display = ('path', 'occurrence_count', 'first_seen_at')
    search_fields = ('path',)
    actions = ['approve_and_save_paths', 'reject_and_ignore_paths']

    @admin.action(description="Approve selected paths (Move to SAVED & parse)")
    def approve_and_save_paths(self, request, queryset):
        count, _ = update_reviewed_paths(
            queryset,
            'pubchem_saved_paths.json',
        )
        self.message_user(
            request,
            f"Successfully approved {count} path(s) and added to saved config.",
        )

    @admin.action(description="Reject selected paths (Move to IGNORED & purge)")
    def reject_and_ignore_paths(self, request, queryset):
        count, _ = update_reviewed_paths(
            queryset,
            'pubchem_ignored_paths.json',
        )
        self.message_user(
            request,
            f"Successfully rejected {count} path(s) and added to ignored config.",
        )

    @staticmethod
    def update_reviewed_paths(queryset, filename):
        config_file = os.path.join(
            settings.BASE_DIR,
            'apps',
            'registry',
            'data',
            filename,
            )

        try:
            with open(config_file, 'r', encoding='utf-8') as file:
                paths = json.load(file)
        except FileNotFoundError:
            paths = []

        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise ValueError(f'Expected a JSON list of strings in {config_file}')

        selected_paths = list(queryset.values_list('path', flat=True))
        existing = set(paths)
        new_paths = list(dict.fromkeys(
            path for path in selected_paths if path not in existing
        ))
        updated_paths = [*paths, *new_paths]

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                dir=os.path.dirname(config_file),
                prefix='.reviewed-paths-',
                suffix='.tmp',
                delete=False,
            ) as file:
                temp_path = file.name
                json.dump(updated_paths, file, indent=2)

            os.replace(temp_path, config_file)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

        deleted_count, _ = queryset.delete()
        return len(new_paths), deleted_count

@admin.register(ProviderTaskLog)
class ProviderTaskLogAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'provider',
        'status',
        'total_items',
        'processed_items',
        'success_count',
        'not_found_count',
        'error_count',
        'started_at',
        'completed_at'
    )
    list_filter = ('status', 'provider', 'started_at')
    search_fields = ('provider', 'log_summary')
    readonly_fields = (
        'started_at',
        'completed_at',
        'total_items',
        'processed_items',
        'success_count',
        'not_found_count',
        'error_count',
        'log_summary'
    )
    ordering = ('-started_at',)

class DataGatheringRunAdminForm(forms.ModelForm):
    class Meta:
        model = DataGatheringRun
        fields = (
            'provider_code',
            'scheduled_for',
            'max_runtime_minutes',
            'batch_size',
        )

    def clean_provider_code(self):
        provider_code = self.cleaned_data['provider_code'].strip().lower()

        if not WebSource.objects.filter(
            is_active=True,
            provider_code__iexact=provider_code,
        ).exists():
            raise forms.ValidationError(
                'Choose the provider_code of an active WebSource.'
            )

        active_runs = DataGatheringRun.objects.filter(
            provider_code__iexact=provider_code,
            status__in=(
                DataGatheringRun.Status.SCHEDULED,
                DataGatheringRun.Status.RUNNING,
            ),
        )
        if self.instance.pk:
            active_runs = active_runs.exclude(pk=self.instance.pk)

        if active_runs.exists():
            raise forms.ValidationError(
                'This provider already has a scheduled or running data-gathering run.'
            )

        return provider_code

    def clean_max_runtime_minutes(self):
        value = self.cleaned_data['max_runtime_minutes']
        if value < 1:
            raise forms.ValidationError('Runtime must be at least one minute.')
        return value

    def clean_batch_size(self):
        value = self.cleaned_data['batch_size']
        if value < 1:
            raise forms.ValidationError('Batch size must be at least one.')
        return value


@admin.register(DataGatheringRun)
class DataGatheringRunAdmin(admin.ModelAdmin):
    form = DataGatheringRunAdminForm
    list_display = (
        'provider_code',
        'status',
        'scheduled_for',
        'started_at',
        'finished_at',
        'processed_count',
        'batch_size',
        'pause_reason',
    )
    list_filter = ('status', 'provider_code')
    search_fields = ('provider_code', 'pause_reason')
    ordering = ('-created_at',)
    actions = ('pause_runs', 'resume_runs')

    readonly_fields = (
        'status',
        'started_at',
        'finished_at',
        'processed_count',
        'pause_reason',
        'rate_limit_retries',
        'dispatched_at',
        'created_at',
    )

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return self.readonly_fields + (
                'provider_code',
                'scheduled_for',
                'max_runtime_minutes',
                'batch_size',
            )
        return self.readonly_fields

    @admin.action(description='Pause selected scheduled/running runs')
    def pause_runs(self, request, queryset):
        count = queryset.filter(
            status__in=(
                DataGatheringRun.Status.SCHEDULED,
                DataGatheringRun.Status.RUNNING,
            )
        ).update(
            status=DataGatheringRun.Status.PAUSED,
            finished_at=timezone.now(),
            pause_reason='Manually paused by admin.',
        )
        self.message_user(request, f'Paused {count} run(s).')

    @admin.action(description='Resume selected paused runs')
    def resume_runs(self, request, queryset):
        count = queryset.filter(
            status=DataGatheringRun.Status.PAUSED,
        ).update(
            status=DataGatheringRun.Status.SCHEDULED,
            scheduled_for=timezone.now(),
            started_at=None,
            finished_at=None,
            pause_reason='',
            dispatched_at=None,
            rate_limit_retries=0,
        )
        self.message_user(
            request,
            f'Rescheduled {count} run(s) to start as soon as Beat dispatches them.',
        )
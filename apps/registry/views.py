import logging
import os
import json
import uuid
import csv
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile
from datetime import datetime
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import (
    LoginView,
    PasswordResetView,
    PasswordResetDoneView,
    PasswordResetConfirmView,
    PasswordResetCompleteView,
)
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.http import (
    FileResponse,
    JsonResponse,
    HttpResponse,
    Http404,
    StreamingHttpResponse,
)
from django.template.loader import render_to_string
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.encoding import force_str, force_bytes
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.db.models import Count, Q
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.views.decorators.http import require_POST, require_GET

from .forms import SignUpForm, ContactForm, UsernamePasswordResetForm
from .email_utils import send_activation_email, send_activation_confirmation
from .models import (
    ChemicalGroup,
    Structure,
    SearchTerm,
    SearchTask,
    Source,
    SourceImport,
    WebSource,
    ProviderTaskLog,
    DataGatheringRun,
)
from .services.parser import (
    get_csv_preview_rows,
    parse_and_stage_file,
)
from .tasks import process_source_import, stage_source_import
from .services import structure_curation
from .services import chemical_lookup
from .services import search_term_review
from .services.provider_run_control import (
    cancel_provider_run,
    dismiss_provider_run,
)
from .tasks import (
    collect_structure_curation_preview,
    commit_structure_curation_preview,
)

logger = logging.getLogger(__name__)

# ==========================================
# GENERAL PUBLIC & ACCOUNT VIEWS
# ==========================================

def home_view(request):
    return render(
        request,
        'core/home.html',
        {
            'database_stats': {
                'search_terms': SearchTerm.objects.count(),
                'search_tasks': SearchTask.objects.count(),
                'successful_tasks': SearchTask.objects.filter(
                    status=SearchTask.TaskStatus.SUCCESS_FOUND,
                ).count(),
                'unique_structures': Structure.objects.count(),
            },
        },
    )

def description_view(request):
    return render(request, 'core/description.html')

@staff_member_required
def instructions_view(request):
    return render(request, 'core/instructions.html')

def changelog_view(request):
    return render(request, 'core/changelog.html')

def thanks_view(request):
    return render(request, 'core/thanks.html')

class CustomLoginView(LoginView):
    template_name = 'core/login.html'
    def get_success_url(self):
        return reverse('home')


class UsernamePasswordResetView(PasswordResetView):
    form_class = UsernamePasswordResetForm
    template_name = 'core/password_reset_form.html'
    email_template_name = 'core/password_reset_email.html'
    subject_template_name = 'core/password_reset_subject.txt'
    success_url = reverse_lazy('password_reset_done')

    def get_initial(self):
        initial = super().get_initial()
        initial['email'] = self.request.GET.get('username', '')
        return initial


class CustomPasswordResetDoneView(PasswordResetDoneView):
    template_name = 'core/password_reset_done.html'


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'core/password_reset_confirm.html'
    success_url = reverse_lazy('password_reset_complete')


class CustomPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = 'core/password_reset_complete.html'


def signup(request):
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                username=form.cleaned_data['username'],
                email=form.cleaned_data['email'],
                password=form.cleaned_data['password1'],
                first_name=form.cleaned_data['first_name'],
                last_name=form.cleaned_data['last_name'],
                is_active=False
            )
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            relative_activate_url = reverse('activate', args=[uid, token])
            full_url = request.build_absolute_uri(relative_activate_url)

            send_activation_email(user, full_url, form.cleaned_data.get('reason'))
            send_activation_confirmation(user.email)
            return redirect('thanks')
    else:
        form = SignUpForm()
    return render(request, 'core/signup.html', {'form': form})

@login_required
def account_details_view(request):
    return render(request, 'core/account_details.html', {'user': request.user})

@login_required
def search_view(request):
    if request.method == 'POST':
        old_token = request.session.get('chemical_lookup_token')
        if old_token:
            chemical_lookup.discard_upload(old_token)
        request.session.pop('chemical_lookup_token', None)

        source_mode = request.POST.get('source_mode')
        try:
            if source_mode == 'paste':
                token, path, delimiter = chemical_lookup.stage_pasted_text(
                    request.POST.get('pasted_rows', ''),
                    request.POST.get('delimiter', 'auto'),
                )
                has_headers = request.POST.get('paste_has_headers') == 'on'
            elif source_mode == 'file':
                upload = request.FILES.get('input_file')
                if upload is None:
                    raise ValueError('Choose a CSV, TSV, or Excel file to upload.')
                token, path = chemical_lookup.stage_uploaded_file(upload)
                delimiter = request.POST.get('delimiter', 'auto')
                has_headers = request.POST.get('file_has_headers') == 'on'
            else:
                raise ValueError('Choose pasted rows or a file as your input.')

            headers, rows = chemical_lookup.read_input_file(
                path,
                has_headers=has_headers,
                delimiter=delimiter,
            )
            if not headers:
                raise ValueError('No columns were found in the submitted input.')
            if not rows:
                raise ValueError('No chemical rows were found in the submitted input.')
        except ValueError as exc:
            if 'token' in locals():
                chemical_lookup.discard_upload(token)
            messages.error(request, str(exc))
            return redirect('search')

        request.session['chemical_lookup_token'] = token
        request.session['chemical_lookup_headers'] = headers
        request.session['chemical_lookup_has_headers'] = has_headers
        request.session['chemical_lookup_delimiter'] = delimiter
        return redirect('chemical_lookup_map')

    return render(request, 'core/search.html')


@login_required
def chemical_lookup_map_view(request):
    token = request.session.get('chemical_lookup_token')
    headers = request.session.get('chemical_lookup_headers')
    if not token or not headers:
        messages.error(request, 'Start a chemical lookup before mapping columns.')
        return redirect('search')

    try:
        path = chemical_lookup.uploaded_path(token)
        headers, rows = chemical_lookup.read_input_file(
            path,
            has_headers=request.session.get('chemical_lookup_has_headers', True),
            delimiter=request.session.get('chemical_lookup_delimiter', 'auto'),
        )
    except ValueError as exc:
        request.session.pop('chemical_lookup_token', None)
        messages.error(request, str(exc))
        return redirect('search')

    suggestions = {
        index: chemical_lookup.suggest_identifier_type(header)
        for index, header in enumerate(headers)
    }
    columns = [
        {
            'index': index,
            'header': header,
            'suggestion': suggestions[index],
            'selected_type': request.POST.get(
                f'column_{index}',
                suggestions[index],
            ),
            'samples': [
                row.get(f'column_{index}', '')
                for row in rows[:5]
            ],
        }
        for index, header in enumerate(headers)
    ]
    if request.method == 'POST':
        column_mapping = {
            str(index): request.POST.get(f'column_{index}', '')
            for index in range(len(headers))
            if request.POST.get(f'column_{index}', '')
        }
        data_sections = request.POST.getlist('data_sections')
        file_format = request.POST.get('file_format', '')
        collapse_mode = request.POST.get('collapse_mode', '')
        source_list_column = request.POST.get('source_list_column', '')
        try:
            if not column_mapping:
                raise ValueError('Map at least one column to an identifier type.')
            if not data_sections:
                raise ValueError('Select at least one data section for your report.')
            if file_format not in {'csv', 'xlsx'}:
                raise ValueError('Choose CSV or Excel as the required download format.')
            if collapse_mode not in {'', 'exact', 'parent'}:
                raise ValueError('Choose a valid duplicate-collapse option.')

            payload = chemical_lookup.resolve_chemical_rows(
                rows,
                column_mapping,
                data_sections,
            )
            search_term_review.queue_unmatched_search_terms(
                payload,
                request.user,
            )
            if collapse_mode:
                (
                    output_headers,
                    output_rows,
                    lineage_headers,
                    lineage_rows,
                ) = chemical_lookup.export_collapsed_rows(
                    payload,
                    headers,
                    collapse_mode,
                    source_list_column,
                )
            else:
                output_headers, output_rows = chemical_lookup.export_rows(
                    payload,
                    headers,
                    file_format,
                )
        except (ValueError, RuntimeError) as exc:
            messages.error(request, str(exc))
        else:
            chemical_lookup.discard_upload(token)
            request.session.pop('chemical_lookup_token', None)
            request.session.pop('chemical_lookup_headers', None)
            request.session.pop('chemical_lookup_has_headers', None)
            request.session.pop('chemical_lookup_delimiter', None)
            filename = f'chemical-lookup-results.{file_format}'
            if collapse_mode and file_format == 'csv':
                def csv_content(fieldnames, values):
                    output = StringIO(newline='')
                    writer = csv.DictWriter(output, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(values)
                    return output.getvalue()

                output = BytesIO()
                with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
                    archive.writestr(
                        'chemical-lookup-collapsed.csv',
                        csv_content(output_headers, output_rows),
                    )
                    archive.writestr(
                        'chemical-lookup-lineage.csv',
                        csv_content(lineage_headers, lineage_rows),
                    )
                response = HttpResponse(
                    output.getvalue(),
                    content_type='application/zip',
                )
                filename = 'chemical-lookup-collapsed.zip'
            elif file_format == 'csv':
                output = StringIO(newline='')
                writer = csv.DictWriter(output, fieldnames=output_headers)
                writer.writeheader()
                writer.writerows(output_rows)
                response = HttpResponse(
                    output.getvalue(),
                    content_type='text/csv; charset=utf-8',
                )
            else:
                from openpyxl import Workbook

                workbook = Workbook(write_only=True)
                worksheet = workbook.create_sheet(
                    'Collapsed results' if collapse_mode else 'Chemical results'
                )
                worksheet.append(output_headers)
                for row in output_rows:
                    worksheet.append([row[header] for header in output_headers])
                if collapse_mode:
                    lineage_worksheet = workbook.create_sheet('Row lineage')
                    lineage_worksheet.append(lineage_headers)
                    for row in lineage_rows:
                        lineage_worksheet.append([
                            row[header] for header in lineage_headers
                        ])
                output = BytesIO()
                workbook.save(output)
                response = HttpResponse(
                    output.getvalue(),
                    content_type=(
                        'application/vnd.openxmlformats-officedocument.'
                        'spreadsheetml.sheet'
                    ),
                )
                if collapse_mode:
                    filename = 'chemical-lookup-collapsed.xlsx'
            response['Content-Disposition'] = (
                f'attachment; filename="{filename}"'
            )
            return response

    return render(
        request,
        'core/chemical_lookup_map.html',
        {
            'headers': headers,
            'columns': columns,
            'preview_rows': rows[:5],
            'identifier_types': chemical_lookup.IDENTIFIER_TYPES,
            'suggestions': suggestions,
            'section_options': [
                ('structure', 'Structure and molecular properties'),
                ('identifiers', 'Known identifiers and names'),
                ('physchem', 'Physicochemical properties'),
                ('activities', 'Biological and toxicological activities'),
            ],
            'selected_sections': (
                request.POST.getlist('data_sections')
                if request.method == 'POST'
                else ['structure', 'identifiers']
            ),
            'selected_format': request.POST.get('file_format', ''),
            'selected_collapse_mode': request.POST.get('collapse_mode', ''),
            'selected_source_list_column': request.POST.get(
                'source_list_column',
                '',
            ),
        },
    )

def contact_view(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            name = form.cleaned_data['name']
            sender_email = form.cleaned_data['email']
            subject = form.cleaned_data['subject']
            message_body = form.cleaned_data['message']

            full_subject = f"[EDiT Support] {subject}"
            email_content = f"Message from: {name} ({sender_email})\n\n{message_body}"
            admin_email = getattr(settings, 'ADMIN_EMAIL', getattr(settings, 'DEFAULT_FROM_EMAIL', 'admin@example.com'))

            try:
                send_mail(full_subject, email_content, settings.DEFAULT_FROM_EMAIL, [admin_email], fail_silently=False)
                messages.success(request, "Your message has been successfully sent to the system administrator.")
                return redirect('contact')
            except Exception as e:
                messages.error(request, f"An error occurred while sending your message: {e}")
    else:
        initial_data = {}
        if request.user.is_authenticated:
            initial_data['name'] = request.user.get_full_name() or request.user.username
            initial_data['email'] = request.user.email
        form = ContactForm(initial=initial_data)

    return render(request, 'core/contact.html', {'form': form})


# ==========================================
# 4-STEP CSV IMPORT WIZARD VIEWS
# ==========================================

@staff_member_required
def import_upload_view(request):
    if request.method == 'POST':
        spreadsheet_file = request.FILES.get('csv_file')
        valid_extensions = ('.csv', '.xlsx', '.xls')

        if (
            not spreadsheet_file
            or not spreadsheet_file.name.lower().endswith(valid_extensions)
        ):
            messages.error(request, 'Please upload a valid .csv, .xlsx, or .xls file.')
            return redirect('import_upload')

        try:
            file_path, file_hash, headers, encoding, original_filename = (
                parse_and_stage_file(spreadsheet_file)
            )

            source, _ = Source.objects.get_or_create(
                source_name=original_filename,
                defaults={
                    'source_description': (
                        'Imported via management wizard (CSV/Excel).'
                    ),
                    'original_file_path': file_path,
                },
            )

            source_import = SourceImport.objects.create(
                source=source,
                file_hash=file_hash,
                file_path=file_path,
                file_encoding=encoding,
                original_filename=original_filename,
                headers=headers,
            )
            return redirect('import_map', source_import_id=source_import.pk)

        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('import_upload')
        except Exception:
            logger.exception('Could not save uploaded import file')
            messages.error(
                request,
                'The file could not be saved or inspected. Please try again '
                'or contact an administrator.',
            )
            return redirect('import_upload')

    return render(request, 'core/import_upload.html')

@staff_member_required
def import_map_view(request, source_import_id):
    source_import = get_object_or_404(SourceImport, pk=source_import_id)

    if source_import.staging_status == SourceImport.StagingStatus.READY:
        return redirect('import_preview', source_import_id=source_import.pk)

    if source_import.staging_status == SourceImport.StagingStatus.PROCESSING:
        return redirect('import_status', source_import_id=source_import.pk)

    headers = source_import.headers

    if request.method == 'POST':
        mapping = {}

        for header in headers:
            category = request.POST.get(f'mapping_{header}')
            subtype = request.POST.get(f'sub_{header}')

            if subtype == 'CUSTOM':
                subtype = (
                    request.POST.get(f'custom_sub_{header}', '').strip()
                    or 'OTHER'
                )

            if not category:
                continue

            if category in (
                'SMILES_CANONICAL',
                'SMILES_ISOMERIC',
                'INCHIKEY',
                'INCHI',
            ):
                mapping[header] = {
                    'category': category,
                    'subtype': category,
                }
            else:
                mapping[header] = {
                    'category': category,
                    'subtype': subtype or category,
                }

        if not mapping:
            messages.error(request, 'Map at least one column before continuing.')
            return redirect('import_map', source_import_id=source_import.pk)

        source_import.column_mapping = mapping
        source_import.staging_status = SourceImport.StagingStatus.NOT_STARTED
        source_import.staging_error = ''
        source_import.save(
            update_fields=[
                'column_mapping',
                'staging_status',
                'staging_error',
            ]
        )

        try:
            stage_source_import.apply_async(
                args=[str(source_import.pk)],
                queue='registry',
            )
        except Exception as exc:
            logger.exception('Could not queue staging for import %s', source_import.pk)
            source_import.staging_status = SourceImport.StagingStatus.FAILED
            source_import.staging_error = str(exc)
            source_import.save(
                update_fields=['staging_status', 'staging_error']
            )
            messages.error(request, 'Staging could not be queued. Please try again.')
            return redirect('import_map', source_import_id=source_import.pk)

        return redirect('import_status', source_import_id=source_import.pk)

    preview_rows = get_csv_preview_rows(
        source_import.file_path,
        source_import.file_encoding,
        limit=5,
    )

    return render(
        request,
        'core/import_map.html',
        {
            'source_import': source_import,
            'headers': headers,
            'preview_rows': preview_rows,
        },
    )

@staff_member_required
def import_status_view(request, source_import_id):
    source_import = get_object_or_404(SourceImport, pk=source_import_id)

    return render(
        request,
        'core/import_status.html',
        {
            'source_import': source_import,
            'status_url': reverse(
                'import_status_data',
                args=[source_import.pk],
            ),
            'preview_url': reverse(
                'import_preview',
                args=[source_import.pk],
            ),
            'admin_url': reverse(
                'admin:registry_sourceimport_change',
                args=[source_import.pk],
            ),
        },
    )


@staff_member_required
@require_GET
def import_status_data_view(request, source_import_id):
    source_import = get_object_or_404(SourceImport, pk=source_import_id)

    return JsonResponse({
        'staging_status': source_import.staging_status,
        'import_status': source_import.import_status,
        'staged_rows': source_import.staged_row_count,
        'row_count': source_import.row_count,
        'progress_message': source_import.progress_message,
        'error': (
            source_import.error_log
            if source_import.import_status == SourceImport.ImportStatus.FAILED
            else source_import.staging_error
        ),
    })


@staff_member_required
def import_preview_view(request, source_import_id):
    source_import = get_object_or_404(SourceImport, pk=source_import_id)

    if source_import.staging_status != SourceImport.StagingStatus.READY:
        return redirect('import_status', source_import_id=source_import.pk)

    staged_query = source_import.staged_rows.all()

    # These are database counts, not a Python list of the entire import.
    new_count = staged_query.filter(
        is_valid=True,
        is_existing=False,
    ).count()
    existing_count = staged_query.filter(
        is_valid=True,
        is_existing=True,
    ).count()
    invalid_count = staged_query.filter(
        is_valid=False,
    ).count()

    paginator = Paginator(staged_query, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    staged_rows = [
        json.loads(staged_row.staged_data)
        for staged_row in page_obj.object_list
    ]

    return render(
        request,
        'core/import_preview.html',
        {
            'source_import': source_import,
            'page_obj': page_obj,
            'staged_rows': staged_rows,
            'total_rows': source_import.staged_row_count,
            'new_count': new_count,
            'existing_count': existing_count,
            'invalid_count': invalid_count,
        },
    )


@staff_member_required
@require_POST
def import_commit_view(request, source_import_id):
    source_import = get_object_or_404(SourceImport, pk=source_import_id)

    if source_import.staging_status != SourceImport.StagingStatus.READY:
        messages.error(request, 'This import is not ready to commit.')
        return redirect('import_status', source_import_id=source_import.pk)

    if source_import.import_status in (
        SourceImport.ImportStatus.QUEUED,
        SourceImport.ImportStatus.PROCESSING,
        SourceImport.ImportStatus.SUCCESS,
    ):
        messages.info(request, 'This import has already been queued or completed.')
        return redirect('import_status', source_import_id=source_import.pk)

    group_name = request.POST.get('group_name', '').strip()
    if group_name:
        group, _ = ChemicalGroup.objects.get_or_create(
            name=group_name,
            defaults={
                'description': (
                    f'Created via import of {source_import.original_filename}'
                ),
            },
        )
        source_import.chemical_group = group

    source_import.import_status = SourceImport.ImportStatus.QUEUED
    source_import.progress_message = 'Waiting for an import worker to start.'
    source_import.error_log = ''
    source_import.save(
        update_fields=[
            'chemical_group',
            'import_status',
            'progress_message',
            'error_log',
        ]
    )

    try:
        process_source_import.apply_async(
            args=[str(source_import.pk)],
            queue='registry',
        )
    except Exception as exc:
        logger.exception('Could not queue import %s', source_import.pk)
        source_import.import_status = SourceImport.ImportStatus.FAILED
        source_import.progress_message = 'The import could not be queued.'
        source_import.error_log = str(exc)
        source_import.save(
            update_fields=[
                'import_status',
                'progress_message',
                'error_log',
            ]
        )
        messages.error(request, 'The import could not be queued. Please try again.')
        return redirect('import_preview', source_import_id=source_import.pk)

    return redirect('import_status', source_import_id=source_import.pk)

# ==========================================
# DATA GATHERING CENTRE
# ==========================================

@staff_member_required
def data_gathering_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'run':
            provider_filter = request.POST.get('provider_filter', '')
            try:
                batch_size = int(request.POST.get('batch_size', 50))
                max_runtime_minutes = int(
                    request.POST.get('max_runtime_minutes', 360)
                )
            except (TypeError, ValueError):
                messages.error(
                    request,
                    'Batch size and maximum runtime must be whole numbers.',
                )
                return redirect(request.path)

            if not 1 <= batch_size <= 500:
                messages.error(request, 'Batch size must be between 1 and 500.')
                return redirect(request.path)
            if not 1 <= max_runtime_minutes <= 1440:
                messages.error(
                    request,
                    'Maximum runtime must be between 1 and 1440 minutes.',
                )
                return redirect(request.path)
            
            max_tasks_value = request.POST.get('max_tasks', '').strip()
            try:
                max_tasks = int(max_tasks_value) if max_tasks_value else None
            except ValueError:
                messages.error(request, 'Maximum tasks must be a whole number.')
                return redirect(request.path)

            if max_tasks is not None and not 1 <= max_tasks <= 100000:
                messages.error(request, 'Maximum tasks must be between 1 and 100000.')
                return redirect(request.path)

            force_chemspider_small_batches = (
                request.POST.get('force_chemspider_small_batches') == 'on'
            )

            schedule_value = request.POST.get('scheduled_for', '').strip()
            if schedule_value:
                try:
                    scheduled_for = datetime.fromisoformat(schedule_value)
                except ValueError:
                    messages.error(
                        request,
                        'Enter a valid scheduled date and time.',
                    )
                    return redirect(request.path)

                if timezone.is_aware(scheduled_for):
                    messages.error(
                        request,
                        'Scheduled time must be entered as local date and time.',
                    )
                    return redirect(request.path)
                scheduled_for = timezone.make_aware(
                    scheduled_for,
                    timezone.get_current_timezone(),
                )
                if scheduled_for <= timezone.now():
                    messages.error(
                        request,
                        'Scheduled time must be in the future.',
                    )
                    return redirect(request.path)
            else:
                scheduled_for = timezone.now()

            providers = WebSource.objects.filter(is_active=True)
            if provider_filter == 'all_except_chemspider':
                providers = providers.exclude(provider_code__iexact='chemspider')
            elif provider_filter != 'all_active':
                providers = providers.filter(
                    provider_code__iexact=provider_filter,
                )

            with transaction.atomic():
                providers = list(providers.select_for_update().order_by('pk'))
                if not providers:
                    messages.error(
                        request,
                        'No active provider matched that selection.',
                    )
                    return redirect(request.path)

                created_count = 0
                already_running = []
                for provider in providers:
                    active_run = DataGatheringRun.objects.filter(
                        provider_code__iexact=provider.provider_code,
                        status__in=(
                            DataGatheringRun.Status.SCHEDULED,
                            DataGatheringRun.Status.RUNNING,
                        ),
                    ).exists()
                    if active_run:
                        already_running.append(provider.name)
                        continue

                    DataGatheringRun.objects.create(
                        provider_code=provider.provider_code.lower(),
                        scheduled_for=scheduled_for,
                        max_runtime_minutes=max_runtime_minutes,
                        batch_size=batch_size,
                        max_tasks=max_tasks,
                        force_chemspider_small_batches=(
                            force_chemspider_small_batches
                            and provider.provider_code.lower() == 'chemspider'
                        ),
                    )
                    created_count += 1

            if created_count:
                scheduled_label = timezone.localtime(scheduled_for).strftime(
                    '%Y-%m-%d %H:%M %Z'
                )
                messages.success(
                    request,
                    f'Scheduled {created_count} provider run(s) for '
                    f'{scheduled_label}. Celery Beat will dispatch them '
                    'when due.',
                )
            if already_running:
                messages.warning(
                    request,
                    'Skipped providers that already have a scheduled or '
                    f'running job: {", ".join(already_running)}.',
                )
            if not created_count and not already_running:
                messages.info(
                    request,
                    'No provider runs were created.',
                )
            return redirect(request.path)

        if action == 'delete_scheduled_run':
            run_id = request.POST.get('run_id')
            with transaction.atomic():
                try:
                    run = DataGatheringRun.objects.select_for_update().get(
                        pk=run_id,
                    )
                except (
                    DataGatheringRun.DoesNotExist,
                    ValidationError,
                    ValueError,
                ):
                    messages.error(request, 'That provider run could not be found.')
                    return redirect(request.path)

                if (
                    run.status != DataGatheringRun.Status.SCHEDULED
                    or run.started_at is not None
                    or run.dispatched_at is not None
                    or run.processed_count != 0
                ):
                    messages.error(
                        request,
                        'Only scheduled runs that have not been dispatched can '
                        'be deleted. Pause the run if it has already started.',
                    )
                    return redirect(request.path)

                provider_code = run.provider_code
                run.delete()
                messages.success(
                    request,
                    f'The scheduled {provider_code} run was deleted.',
                )
            return redirect(request.path)

        if action in ('pause_run', 'resume_run'):
            run_id = request.POST.get('run_id')
            with transaction.atomic():
                try:
                    run = DataGatheringRun.objects.select_for_update().get(
                        pk=run_id,
                    )
                except (
                    DataGatheringRun.DoesNotExist,
                    ValidationError,
                    ValueError,
                ):
                    messages.error(request, 'That provider run could not be found.')
                    return redirect(request.path)

                if action == 'pause_run' and run.status in (
                    DataGatheringRun.Status.SCHEDULED,
                    DataGatheringRun.Status.RUNNING,
                ):
                    run.status = DataGatheringRun.Status.PAUSED
                    run.finished_at = timezone.now()
                    run.pause_reason = 'Manually paused by operator.'
                    run.save(
                        update_fields=['status', 'finished_at', 'pause_reason']
                    )
                    messages.success(
                        request,
                        f'{run.provider_code} was paused. Any current batch '
                        'will finish before processing stops.',
                    )
                elif action == 'resume_run' and run.status == DataGatheringRun.Status.PAUSED:
                    run.status = DataGatheringRun.Status.SCHEDULED
                    run.scheduled_for = timezone.now()
                    run.started_at = None
                    run.finished_at = None
                    run.pause_reason = ''
                    run.dispatched_at = None
                    run.rate_limit_retries = 0
                    run.save(
                        update_fields=[
                            'status',
                            'scheduled_for',
                            'started_at',
                            'finished_at',
                            'pause_reason',
                            'dispatched_at',
                            'rate_limit_retries',
                        ]
                    )
                    messages.success(
                        request,
                        f'{run.provider_code} was rescheduled to resume.',
                    )
                else:
                    messages.info(
                        request,
                        f'{run.provider_code} is already {run.get_status_display().lower()}.',
                    )
            return redirect(request.path)

        if action == 'cancel_run':
            run_id = request.POST.get('run_id')
            reason = request.POST.get('cancel_reason', '')
            try:
                run, cancelled_logs, requeued_tasks = cancel_provider_run(
                    run_id,
                    reason,
                )
            except ValidationError as exc:
                messages.error(request, ' '.join(exc.messages))
            except (
                DataGatheringRun.DoesNotExist,
                ValueError,
            ) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f'Cancelled {run.provider_code}. '
                    f'Returned {requeued_tasks:,} processing task(s) to pending; '
                    f'cancelled {cancelled_logs:,} active batch log(s).',
                )
            return redirect(request.path)

        if action == 'dismiss_run':
            run_id = request.POST.get('run_id')
            try:
                run = dismiss_provider_run(run_id)
            except ValidationError as exc:
                messages.error(request, ' '.join(exc.messages))
            except (
                DataGatheringRun.DoesNotExist,
                ValueError,
            ) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f'Dismissed the {run.provider_code} run from this dashboard. '
                    'Its run history and provider logs are retained.',
                )
            return redirect(request.path)

        messages.error(request, 'Choose a valid data-gathering action.')
        return redirect(request.path)

    context = _data_gathering_dashboard_context()
    return render(request, 'core/data_gathering.html', context)


def _data_gathering_dashboard_context():
    total_tasks = SearchTask.objects.count()
    pending_tasks = SearchTask.objects.filter(status=SearchTask.TaskStatus.PENDING).count()
    processing_tasks = SearchTask.objects.filter(status=SearchTask.TaskStatus.PROCESSING).count()
    successful_tasks = SearchTask.objects.filter(status=SearchTask.TaskStatus.SUCCESS_FOUND).count()
    not_found_tasks = SearchTask.objects.filter(status=SearchTask.TaskStatus.NOT_FOUND).count()
    error_tasks = SearchTask.objects.filter(status=SearchTask.TaskStatus.ERROR, run_count__lt=3).count()
    exhausted_error_tasks = SearchTask.objects.filter(
        status=SearchTask.TaskStatus.ERROR,
        run_count__gte=3,
    ).count()
    skipped_tasks = SearchTask.objects.filter(
        status=SearchTask.TaskStatus.SKIPPED,
    ).count()

    source_stats = WebSource.objects.annotate(
        total_tasks=Count('tasks'),
        pending_count=Count('tasks', filter=Q(tasks__status='PENDING')),
        processing_count=Count('tasks', filter=Q(tasks__status='PROCESSING')),
        success_count=Count('tasks', filter=Q(tasks__status='SUCCESS_FOUND')),
        not_found_count=Count('tasks', filter=Q(tasks__status='NOT_FOUND')),
        retryable_error_count=Count(
            'tasks',
            filter=Q(tasks__status='ERROR', tasks__run_count__lt=3),
        ),
        exhausted_error_count=Count(
            'tasks',
            filter=Q(tasks__status='ERROR', tasks__run_count__gte=3),
        ),
        skipped_count=Count('tasks', filter=Q(tasks__status='SKIPPED')),
    ).order_by('name')
    active_runs = list(DataGatheringRun.objects.filter(
        status__in=(
            DataGatheringRun.Status.SCHEDULED,
            DataGatheringRun.Status.RUNNING,
        ),
    ).order_by('scheduled_for', 'created_at'))
    active_provider_codes = [run.provider_code.lower() for run in active_runs]
    current_batch_by_provider = {}
    if active_provider_codes:
        running_logs = ProviderTaskLog.objects.filter(
            status='RUNNING',
            provider__in=active_provider_codes,
        ).order_by('-started_at')
        for log in running_logs:
            current_batch_by_provider.setdefault(log.provider.lower(), log)
    for run in active_runs:
        run.current_batch = current_batch_by_provider.get(run.provider_code.lower())
        run.total_processed = run.processed_count + (
            run.current_batch.processed_items if run.current_batch else 0
        )
    paused_runs = DataGatheringRun.objects.filter(
        status=DataGatheringRun.Status.PAUSED,
    ).order_by('-finished_at', '-created_at')[:10]
    terminal_runs = DataGatheringRun.objects.filter(
        status__in=(
            DataGatheringRun.Status.FAILED,
            DataGatheringRun.Status.CANCELLED,
        ),
        is_dismissed=False,
    ).order_by('-finished_at', '-created_at')[:10]
    recent_logs = ProviderTaskLog.objects.order_by('-started_at')[:10]
    has_running_logs = ProviderTaskLog.objects.filter(
        status='RUNNING',
    ).exists()
    has_running_work = (
        has_running_logs
        or any(run.status == DataGatheringRun.Status.RUNNING for run in active_runs)
    )
    has_active_work = (
        bool(active_runs)
        or has_running_logs
    )

    return {
        'total_tasks': total_tasks,
        'pending_tasks': pending_tasks,
        'processing_tasks': processing_tasks,
        'successful_tasks': successful_tasks,
        'not_found_tasks': not_found_tasks,
        'error_tasks': error_tasks,
        'exhausted_error_tasks': exhausted_error_tasks,
        'skipped_tasks': skipped_tasks,
        'source_stats': source_stats,
        'active_runs': active_runs,
        'paused_runs': paused_runs,
        'terminal_runs': terminal_runs,
        'recent_logs': recent_logs,
        'has_active_work': has_active_work,
        'dashboard_refresh_interval': (
            2000 if has_running_logs
            else 15000 if active_runs
            else 30000
        ),
        'display_timezone': timezone.get_current_timezone_name(),
    }


@staff_member_required
@require_GET
def data_gathering_status_view(request):
    context = _data_gathering_dashboard_context()
    html = render_to_string(
        'core/partials/data_gathering_status.html',
        context,
        request=request,
    )
    return HttpResponse(html)


# ==========================================
# CHEMICAL STAGING (IN PROGRESS)
# ==========================================

@staff_member_required
def structure_curation_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action in {
            'triage_group',
            'triage_structure',
            'triage_split',
            'commit_preview',
        }:
            run_id = request.POST.get('run_id', '')
            try:
                run_id = str(uuid.UUID(run_id))
                state = structure_curation.read_run_state(run_id)
                if (
                    state['status'] != 'COMPLETED'
                    or not structure_curation.has_current_preview_reports(
                        run_id,
                        state,
                    )
                ):
                    raise ValueError('This preview is no longer available for review.')
                if action == 'commit_preview':
                    if structure_curation.queue_reviewed_preview_commit(run_id):
                        commit_structure_curation_preview.apply_async(
                            args=[run_id],
                            queue='registry',
                        )
                        messages.success(
                            request,
                            'Adding accepted structures in the background. '
                            'Progress is shown below.',
                        )
                    else:
                        messages.info(
                            request,
                            'Accepted structures are already being added or '
                            'have already been added.',
                        )
                elif action == 'triage_group':
                    decision = request.POST.get('decision', '')
                    structure_curation.save_group_decision(
                        run_id,
                        request.POST.get('group_id', ''),
                        decision,
                    )
                    messages.success(
                        request,
                        f'{decision.title()} applied to the review group.',
                    )
                elif action == 'triage_split':
                    split_keys = (
                        structure_curation.save_structure_input_split(
                            run_id,
                            request.POST.get('inchikey', ''),
                        )
                    )
                    messages.success(
                        request,
                        f'Accepted {len(split_keys)} parsed structures as '
                        'separate records.',
                    )
                else:
                    decision = request.POST.get('decision', '')
                    structure_curation.save_structure_decision(
                        run_id,
                        request.POST.get('inchikey', ''),
                        decision,
                        smiles=request.POST.get('edited_smiles') or None,
                        field_edits={
                            field: request.POST.get(field, '')
                            for field in (
                                'molecular_formula',
                                'molecular_weight',
                                'exact_mass',
                            )
                            if field in request.POST
                        },
                    )
                    messages.success(
                        request,
                        f'{decision.title()} saved for the selected structure.',
                    )
            except (
                ValueError,
                RuntimeError,
                OSError,
                KeyError,
                json.JSONDecodeError,
            ) as exc:
                messages.error(request, str(exc))
            except DatabaseError:
                logger.exception(
                    'Could not commit structure-curation preview %s',
                    run_id,
                )
                messages.error(
                    request,
                    'The database could not save this preview. No partial '
                    'structure commit was retained; check the server logs.',
                )
            return redirect(f'{request.path}?run_id={run_id}')

        if action == 'delete_preview':
            run_id = request.POST.get('run_id', '')
            try:
                structure_curation.delete_run(run_id)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                messages.error(
                    request,
                    str(exc),
                )
            else:
                messages.success(
                    request,
                    'The temporary preview report was deleted. '
                    'The incremental scan checkpoint was retained.',
                )
            return redirect(request.path)

        if action == 'retry_finalization':
            run_id = request.POST.get('run_id', '')
            try:
                run_id = str(uuid.UUID(run_id))
                structure_curation.retry_failed_finalization(run_id)
                collect_structure_curation_preview.apply_async(
                    args=[run_id],
                    queue='registry',
                )
            except RuntimeError as exc:
                messages.warning(request, str(exc))
                return redirect(f'{request.path}?run_id={run_id}')
            except Exception as exc:
                logger.exception(
                    'Could not retry finalization for curation scan %s',
                    run_id,
                )
                messages.error(request, str(exc))
                return redirect(f'{request.path}?run_id={run_id}')
            messages.success(
                request,
                'Retrying finalization from saved scan results. Findings '
                'will not be reprocessed and provider searches will not run.',
            )
            return redirect(f'{request.path}?run_id={run_id}')

        if action == 'reprocess_batch':
            previous_run_id = request.POST.get('run_id', '')
            run_id = str(uuid.uuid4())
            try:
                previous_run_id = str(uuid.UUID(previous_run_id))
                structure_curation.create_run(run_id)
                reprocessed_count = (
                    structure_curation.reprocess_uncommitted_batch(
                        previous_run_id,
                        run_id,
                    )
                )
                collect_structure_curation_preview.apply_async(
                    args=[run_id],
                    queue='registry',
                )
            except RuntimeError as exc:
                messages.warning(request, str(exc))
                return redirect(f'{request.path}?run_id={previous_run_id}')
            except Exception as exc:
                structure_curation.mark_run_failed(run_id, exc)
                logger.exception(
                    'Could not reprocess curation batch %s',
                    previous_run_id,
                )
                messages.error(
                    request,
                    'Could not queue the rebuilt preview. Uncommitted findings '
                    'remain pending; check the registry worker and logs.',
                )
                return redirect(f'{request.path}?run_id={previous_run_id}')
            messages.success(
                request,
                f'Reprocessing exactly {reprocessed_count} parsed, '
                'uncommitted finding(s) from this batch with the current '
                'curation rules. Unsupported/unparsed and unrelated pending '
                'findings are excluded; provider searches were not rerun.',
            )
            return redirect(f'{request.path}?run_id={run_id}')

        if action not in {'scan', 'full_scan'}:
            messages.error(request, 'Choose a valid structure-curation action.')
            return redirect(request.path)

        run_id = str(uuid.uuid4())
        try:
            structure_curation.create_run(
                run_id,
                full_scan=(action == 'full_scan'),
            )
            structure_curation.prepare_finding_batch(
                run_id,
                batch_limit=request.POST.get('batch_limit', 1000),
                full_scan=(action == 'full_scan'),
            )
            collect_structure_curation_preview.apply_async(
                args=[run_id],
                queue='registry',
            )
        except RuntimeError as exc:
            messages.warning(request, str(exc))
            return redirect(request.path)
        except Exception as exc:
            structure_curation.mark_run_failed(run_id, exc)
            logger.exception('Could not queue structure-curation scan %s', run_id)
            messages.error(
                request,
                'Could not queue the preview scan. Check the Celery worker and logs.',
            )
            return redirect(request.path)
        return redirect(f'{request.path}?run_id={run_id}')

    run_id = request.GET.get('run_id') or structure_curation.get_latest_run_id()
    run_state = None
    report_is_current = False
    can_retry_finalization = False
    preview_records = []
    parent_records = []
    evidence_records = []
    invalid_records = []
    review_groups = []
    review_queue = {'groups': [], 'ready_new': [], 'already_existing': []}
    ready_to_commit_count = 0
    ready_new_count = 0
    needs_review_count = 0
    curation_batches = structure_curation.get_curation_batches()
    if run_id:
        try:
            run_id = str(uuid.UUID(run_id))
            run_state = structure_curation.read_run_state(run_id)
            can_retry_finalization = (
                structure_curation.can_retry_failed_finalization(
                    run_id,
                    run_state,
                )
            )
            if run_state['status'] == 'COMPLETED':
                report_is_current = structure_curation.has_current_preview_reports(
                    run_id,
                    run_state,
                )
                if report_is_current:
                    committed_keys = set(
                        run_state.get('committed_inchikeys', [])
                    )
                    preview_records = structure_curation.get_preview(run_id)
                    parent_records = structure_curation.get_parent_preview(run_id)
                    evidence_records = structure_curation.get_evidence_preview(run_id)
                    committed_related_keys = set(committed_keys)
                    for record in preview_records:
                        if record['inchikey'] in committed_keys:
                            if record.get('parent_inchikey'):
                                committed_related_keys.add(
                                    record['parent_inchikey']
                                )
                            committed_related_keys.update(
                                component['inchikey']
                                for component in record.get('components', [])
                            )
                    preview_records = [
                        record for record in preview_records
                        if record['inchikey'] not in committed_keys
                    ]
                    parent_records = [
                        record for record in parent_records
                        if record['inchikey'] not in committed_related_keys
                    ]
                    evidence_records = [
                        record for record in evidence_records
                        if record['structure_inchikey'] not in committed_keys
                    ]
                    invalid_records = structure_curation.get_invalid_preview(run_id)
                    review_queue = structure_curation.get_review_queue(run_id)
                    review_groups = review_queue['groups']
                    ready_to_commit_count = (
                        sum(
                            row['triage_decision'] == 'ACCEPTED'
                            and row['inchikey'] not in committed_keys
                            for row in review_queue['ready_new']
                        )
                        + sum(
                            row['triage_decision'] == 'ACCEPTED'
                            and row['inchikey'] not in committed_keys
                            for row in review_queue['already_existing']
                        )
                        + sum(
                            record['triage_decision'] == 'ACCEPTED'
                            and record['inchikey'] not in committed_keys
                            for group in review_groups
                            for record in group.get('records', [])
                        )
                    )
                    ready_new_count = sum(
                        row['triage_decision'] == 'ACCEPTED'
                        and not row.get('already_in_structure_database')
                        and row['inchikey'] not in committed_keys
                        for row in review_queue['ready_new']
                    )
                    needs_review_count = review_queue['needs_review_count']
        except (ValueError, OSError, KeyError, json.JSONDecodeError):
            run_state = None

    return render(
        request,
        'core/structure_curation.html',
        {
            'run_state': run_state,
            'report_is_current': report_is_current,
            'current_report_format_version': (
                structure_curation.REPORT_FORMAT_VERSION
            ),
            'can_retry_finalization': can_retry_finalization,
            'preview_records': preview_records,
            'parent_records': parent_records,
            'evidence_records': evidence_records,
            'invalid_records': invalid_records,
            'review_groups': review_groups,
            'review_queue': review_queue,
            'ready_to_commit_count': ready_to_commit_count,
            'ready_new_count': ready_new_count,
            'needs_review_count': (
                needs_review_count if report_is_current else 0
            ),
            'curation_batches': curation_batches,
            'run_id': run_id if run_state else None,
        },
    )


@staff_member_required
@require_GET
def structure_curation_status_view(request, run_id):
    try:
        state = structure_curation.read_run_state(run_id)
    except (ValueError, OSError, json.JSONDecodeError):
        raise Http404('Structure-curation scan not found.')
    if request.GET.get('state_only'):
        return JsonResponse({'state': state})
    preview_records = (
        structure_curation.get_preview(run_id)
        if (
            state['status'] == 'COMPLETED'
            and structure_curation.has_current_preview_reports(run_id, state)
        ) else []
    )
    evidence_records = (
        structure_curation.get_evidence_preview(run_id)
        if (
            state['status'] == 'COMPLETED'
            and structure_curation.has_current_preview_reports(run_id, state)
        ) else []
    )
    invalid_records = (
        structure_curation.get_invalid_preview(run_id)
        if (
            state['status'] == 'COMPLETED'
            and structure_curation.has_current_preview_reports(run_id, state)
        ) else []
    )
    review_groups = (
        structure_curation.get_review_groups(run_id, state)
        if (
            state['status'] == 'COMPLETED'
            and structure_curation.has_current_preview_reports(run_id, state)
        ) else []
    )
    return JsonResponse({
        'state': state,
        'report_is_current': structure_curation.has_current_preview_reports(
            run_id,
            state,
        ),
        'preview_records': preview_records,
        'review_groups': review_groups,
        'evidence_records': evidence_records,
        'invalid_records': invalid_records,
    })


@staff_member_required
@require_GET
def structure_curation_download_view(request, run_id, report_type):
    if report_type == 'valid':
        report_path = structure_curation.get_report_path(run_id)
        filename = f'prospective-structures-{run_id}.jsonl'
    elif report_type == 'evidence':
        report_path = structure_curation.get_evidence_report_path(run_id)
        filename = f'prospective-structure-evidence-{run_id}.jsonl'
    elif report_type == 'invalid':
        report_path = structure_curation.get_invalid_report_path(run_id)
        filename = f'structure-preview-invalid-{run_id}.jsonl'
    elif report_type == 'parents':
        report_path = structure_curation.get_parent_report_path(run_id)
        filename = f'prospective-parent-structures-{run_id}.jsonl'
    else:
        report_path = None
        filename = ''
    try:
        state = structure_curation.read_run_state(run_id)
    except (ValueError, OSError, json.JSONDecodeError):
        raise Http404('Structure-curation scan not found.')
    if (
        state['status'] != 'COMPLETED'
        or not structure_curation.has_current_preview_reports(run_id, state)
    ):
        raise Http404('The report is not ready.')
    if report_type == 'triaged':
        response = StreamingHttpResponse(
            (
                json.dumps(record, ensure_ascii=False) + '\n'
                for record in structure_curation.iter_triaged_preview(run_id)
            ),
            content_type='application/x-ndjson',
        )
        response['Content-Disposition'] = (
            f'attachment; filename="triaged-structures-{run_id}.jsonl"'
        )
        return response
    if report_type == 'triaged_evidence':
        response = StreamingHttpResponse(
            (
                json.dumps(record, ensure_ascii=False) + '\n'
                for record in structure_curation.iter_triaged_evidence(run_id)
            ),
            content_type='application/x-ndjson',
        )
        response['Content-Disposition'] = (
            f'attachment; filename="triaged-evidence-{run_id}.jsonl"'
        )
        return response
    if report_path is None:
        raise Http404('Unknown report type.')
    if not report_path.is_file():
        raise Http404('The report is not ready.')
    return FileResponse(
        report_path.open('rb'),
        as_attachment=True,
        filename=filename,
        content_type='application/x-ndjson',
    )


# ==========================================
# API & UTILITY VIEWS
# ==========================================

def check_username_api(request):
    username = request.GET.get('username', '').strip().lower()
    email = request.GET.get('email', '').strip().lower()
    response_data = {'username_available': True, 'suggestions': [], 'email_exists': False}

    if username:
        exists = User.objects.filter(username__iexact=username).exists()
        response_data['username_available'] = not exists
        if exists:
            i = 2
            while len(response_data['suggestions']) < 3:
                alt = f"{username}{i}"
                if not User.objects.filter(username__iexact=alt).exists():
                    response_data['suggestions'].append(alt)
                i += 1
    if email:
        response_data['email_exists'] = User.objects.filter(email__iexact=email).exists()

    return JsonResponse(response_data)

def activate(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        logger.warning(f"Invalid activation attempt for uidb64: {uidb64}")
        return render(request, 'core/activation_invalid.html', status=400)

    if user is not None and default_token_generator.check_token(user, token):
        user.is_active = True
        user.save(update_fields=['is_active'])
        greeting_name = user.first_name or user.username
        login_url = request.build_absolute_uri(reverse('login'))
        reply_blurb = (
            f"Hello {greeting_name},\n\n"
            "Your EDiT account has been approved and is ready to use. "
            f"Your username is {user.username}.\n"
            f"You can log in here:\n{login_url}\n\n"
            "Welcome to EDiT."
        )
        return render(
            request,
            'core/activated_success.html',
            {'user': user, 'reply_blurb': reply_blurb, 'login_url': login_url},
        )

    return render(request, 'core/activation_invalid.html', status=400)

@staff_member_required
def update_staged_row_cas_ajax(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            row_index = int(data.get('row_index'))
            resolved_value = data.get('resolved_value', '').strip()

            staged_rows = request.session.get('staged_rows', [])
            for row in staged_rows:
                if row['row_index'] == row_index:
                    row['user_resolved_cas'] = resolved_value
                    break

            request.session['staged_rows'] = staged_rows
            request.session.modified = True
            return JsonResponse({'status': 'success', 'row_index': row_index, 'resolved_value': resolved_value})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    return JsonResponse({'status': 'invalid method'}, status=405)
import importlib
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .base import ProviderAccessDenied, ProviderRateLimited

from apps.registry.models import (
    DataGatheringRun,
    ProviderTaskLog,
    SearchTask,
    WebSource,
)

logger = logging.getLogger(__name__)


def run_provider_batch(
    provider_code,
    batch_size=500,
    force_chemspider_small_batches=False,
    run_id=None,
):
    provider_code = provider_code.strip().lower()

    if run_id is not None and DataGatheringRun.objects.filter(
        pk=run_id,
        status=DataGatheringRun.Status.CANCELLED,
    ).exists():
        cancelled_log = ProviderTaskLog.objects.create(
            provider=provider_code,
            status='CANCELLED',
            completed_at=timezone.now(),
            log_summary='Cancelled before the provider batch started.',
        )
        return cancelled_log, 0, False

    source_ids = list(
        WebSource.objects.filter(
            is_active=True,
            provider_code__iexact=provider_code,
        ).values_list('id', flat=True)
    )
    if not source_ids:
        raise ValueError(f"No active WebSource configured for '{provider_code}'.")

    SearchTask.objects.filter(
        web_source_id__in=source_ids,
        status=SearchTask.TaskStatus.NOT_FOUND,
        error_message__startswith='Max retries (3) reached. Failed with:',
    ).update(
        status=SearchTask.TaskStatus.ERROR,
        is_completed=True,
    )

    if provider_code == 'cactus':
        skipped_count = SearchTask.objects.filter(
            web_source_id__in=source_ids,
            search_term__term_type__iexact='Masked ID',
        ).filter(
            Q(status=SearchTask.TaskStatus.PENDING)
            | Q(status=SearchTask.TaskStatus.ERROR, run_count__lt=3)
        ).update(
            status=SearchTask.TaskStatus.SKIPPED,
            is_completed=True,
            error_message='Skipped by CACTUS: masked identifiers are not searchable.',
        )
        if skipped_count:
            logger.info(
                'Skipped %s unsupported masked-ID tasks for CACTUS.',
                skipped_count,
            )

    eligible = (
        SearchTask.objects
        .filter(web_source_id__in=source_ids)
        .filter(
            Q(status=SearchTask.TaskStatus.PENDING)
            | Q(status=SearchTask.TaskStatus.ERROR, run_count__lt=3)
        )
        .order_by('last_run_at', 'pk')
    )

    with transaction.atomic():
        run_cancelled = False
        if run_id is not None:
            run_status = DataGatheringRun.objects.select_for_update().values_list(
                'status',
                flat=True,
            ).get(pk=run_id)
            run_cancelled = run_status == DataGatheringRun.Status.CANCELLED

        tasks = [] if run_cancelled else list(
            eligible
            .select_for_update(skip_locked=True)
            .select_related('search_term', 'web_source')
            [:batch_size]
        )
        task_ids = [task.pk for task in tasks]

        task_log = ProviderTaskLog.objects.create(
            provider=provider_code,
            status='CANCELLED' if run_cancelled else 'RUNNING',
            total_items=len(tasks),
            completed_at=timezone.now() if run_cancelled else None,
            log_summary='Cancelled before the batch started.' if run_cancelled else '',
        )

        if task_ids:
            SearchTask.objects.filter(pk__in=task_ids).update(
                status=SearchTask.TaskStatus.PROCESSING,
                is_completed=False,
            )
            for task in tasks:
                task.status = SearchTask.TaskStatus.PROCESSING

    if not tasks:
        if task_log.status != 'CANCELLED':
            task_log.status = 'COMPLETED'
            task_log.completed_at = timezone.now()
            task_log.log_summary = 'No pending or retryable tasks found.'
            task_log.save(update_fields=['status', 'completed_at', 'log_summary'])
        return task_log, 0, False

    try:
        provider_module = importlib.import_module(
            f'apps.registry.services.providers.{provider_code}'
        )
        provider_options = {}
        if provider_code == 'chemspider':
            provider_options['force_small_batch'] = force_chemspider_small_batches
        provider_module.execute_batch(tasks, task_log, **provider_options)

        task_log.refresh_from_db()
        if task_log.status == 'CANCELLED':
            SearchTask.objects.filter(
                pk__in=task_ids,
                status=SearchTask.TaskStatus.PROCESSING,
            ).update(
                status=SearchTask.TaskStatus.PENDING,
                is_completed=False,
                error_message='',
            )
            return task_log, task_log.processed_items, True

        # Some providers intentionally defer tasks. Don’t leave those rows
        # permanently stuck in PROCESSING.
        SearchTask.objects.filter(
            pk__in=task_ids,
            status=SearchTask.TaskStatus.PROCESSING,
        ).update(status=SearchTask.TaskStatus.PENDING)

        task_log.status = 'COMPLETED'
        task_log.completed_at = timezone.now()
        task_log.log_summary = (
            f"Processed {task_log.processed_items} tasks. "
            f"Success: {task_log.success_count}, "
            f"Not found: {task_log.not_found_count}, "
            f"Errors: {task_log.error_count}"
        )
        task_log.save(
            update_fields=['status', 'completed_at', 'log_summary']
        )

    except (ProviderRateLimited, ProviderAccessDenied) as exc:
        task_log.refresh_from_db(fields=['status'])
        if task_log.status == 'CANCELLED':
            SearchTask.objects.filter(
                pk__in=task_ids,
                status=SearchTask.TaskStatus.PROCESSING,
            ).update(
                status=SearchTask.TaskStatus.PENDING,
                is_completed=False,
                error_message='',
            )
            return task_log, task_log.processed_items, True

        SearchTask.objects.filter(
            pk__in=task_ids,
            status=SearchTask.TaskStatus.PROCESSING,
        ).update(
            status=SearchTask.TaskStatus.PENDING,
            is_completed=False,
        )

        task_log.status = (
            'RATE_LIMITED'
            if isinstance(exc, ProviderRateLimited)
            else 'ACCESS_DENIED'
        )
        task_log.completed_at = timezone.now()
        task_log.log_summary = str(exc)
        task_log.save()

        raise

    except Exception as exc:
        logger.exception("Resolution batch failed for provider %s", provider_code)
        task_log.refresh_from_db(fields=['status'])
        if task_log.status == 'CANCELLED':
            SearchTask.objects.filter(
                pk__in=task_ids,
                status=SearchTask.TaskStatus.PROCESSING,
            ).update(
                status=SearchTask.TaskStatus.PENDING,
                is_completed=False,
                error_message='',
            )
            return task_log, task_log.processed_items, True

        SearchTask.objects.filter(
            pk__in=task_ids,
            status=SearchTask.TaskStatus.PROCESSING,
        ).update(
            status=SearchTask.TaskStatus.ERROR,
            is_completed=False,
            error_message=f"Batch failed: {exc}"[:2000],
        )
        task_log.refresh_from_db()
        task_log.status = 'FAILED'
        task_log.completed_at = timezone.now()
        task_log.log_summary = f"Batch failed: {exc}"[:2000]
        task_log.save(
            update_fields=['status', 'completed_at', 'log_summary']
        )
        raise

    has_more = eligible.exists()
    return task_log, len(tasks), has_more
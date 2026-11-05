from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.db.models import F
import json

from apps.registry.models import DataGatheringRun, SourceImport, StagedImportRow
from apps.registry.services.base import ProviderAccessDenied, ProviderRateLimited
from apps.registry.services.parser import (
    commit_parsed_import,
    generate_staged_preview,
    iter_staged_preview_chunks,
)
from apps.registry.services.provider_batch import run_provider_batch
from apps.registry.services import structure_curation
import logging

logger = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF_BASE_SECONDS = 15 * 60
RATE_LIMIT_BACKOFF_MAX_SECONDS = 2 * 60 * 60


def rate_limit_backoff_seconds(retry_count, retry_after_seconds):
    backoff = min(
        RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** max(0, retry_count - 1)),
        RATE_LIMIT_BACKOFF_MAX_SECONDS,
    )
    return max(backoff, retry_after_seconds)


@shared_task(
    bind=True,
    name='registry.collect_structure_curation_preview',
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=3,
)
def collect_structure_curation_preview(self, run_id):
    queue_next_chunk = False
    try:
        with structure_curation.run_lock(run_id):
            state = structure_curation.read_run_state(run_id)
            if state['status'] == 'COMPLETED':
                return 'already completed'
            if state['status'] == 'FAILED':
                return 'scan already failed'

            if state['status'] != 'FINALIZING':
                finished = structure_curation.process_scan_chunk(run_id)
                state = structure_curation.read_run_state(run_id)
                queue_next_chunk = not finished

            if not queue_next_chunk:
                structure_curation.finalize_scan(run_id)
        if queue_next_chunk:
            collect_structure_curation_preview.apply_async(
                args=[run_id],
                queue='registry',
                countdown=1,
            )
    except RuntimeError as exc:
        if 'already running' in str(exc):
            return 'scan already running'
        raise
    except Exception as exc:
        logger.exception(
            'Structure curation scan %s failed during preview processing or finalization',
            run_id,
        )
        if self.request.retries >= self.max_retries:
            structure_curation.mark_run_failed(run_id, exc)
            raise
        raise self.retry(exc=exc, countdown=30)

    if queue_next_chunk:
        return 'structure preview chunk queued'
    return 'structure preview completed'


@shared_task(
    bind=True,
    name='registry.commit_structure_curation_preview',
    acks_late=True,
    reject_on_worker_lost=True,
)
def commit_structure_curation_preview(self, run_id):
    try:
        structure_curation.run_reviewed_preview_commit(run_id)
    except RuntimeError as exc:
        if 'already running' in str(exc):
            return 'structure commit skipped: run is busy'
        raise
    except Exception:
        logger.exception(
            'Structure curation commit %s failed',
            run_id,
        )
        raise
    return 'structure preview committed'


@shared_task(name='registry.stage_source_import')
def stage_source_import(source_import_id):
    with transaction.atomic():
        source_import = (
            SourceImport.objects
            .select_for_update(of=('self',))
            .select_related('source', 'chemical_group')
            .get(pk=source_import_id)
        )

        if source_import.staging_status == SourceImport.StagingStatus.READY:
            return 'already staged'

        if source_import.staging_status == SourceImport.StagingStatus.PROCESSING:
            return 'already staging'

        if not source_import.column_mapping:
            source_import.staging_status = SourceImport.StagingStatus.FAILED
            source_import.staging_error = 'No column mapping was saved.'
            source_import.save(
                update_fields=['staging_status', 'staging_error']
            )
            return 'missing column mapping'

        source_import.staging_status = SourceImport.StagingStatus.PROCESSING
        source_import.staging_error = ''
        source_import.staged_row_count = 0
        source_import.save(
            update_fields=[
                'staging_status',
                'staging_error',
                'staged_row_count',
            ]
        )

        # A retry starts over from the beginning, so discard partial staging.
        source_import.staged_rows.all().delete()

    try:
        if not source_import.file_path:
            raise ValueError('The import has no stored file path.')

        for chunk in iter_staged_preview_chunks(
            source_import.file_path,
            source_import.file_encoding,
            source_import.column_mapping,
            chunk_size=1000,
        ):
            staged_objects = [
                StagedImportRow(
                    source_import_id=source_import_id,
                    row_index=row['row_index'],
                    sort_priority=row['sort_priority'],
                    staged_data=json.dumps(row, ensure_ascii=True),
                    is_valid=row['is_valid'],
                    is_existing=row['is_existing'],
                )
                for row in chunk
            ]

            # Commit each bounded chunk separately. If a later chunk fails,
            # earlier chunks remain available for cleanup or a full retry.
            with transaction.atomic():
                StagedImportRow.objects.bulk_create(
                    staged_objects,
                    batch_size=1000,
                )
                SourceImport.objects.filter(
                    pk=source_import_id,
                ).update(
                    staged_row_count=F('staged_row_count') + len(staged_objects)
                )

        SourceImport.objects.filter(pk=source_import_id).update(
            staging_status=SourceImport.StagingStatus.READY,
            staging_error='',
        )

    except Exception as exc:
        logger.exception('Staging import %s failed', source_import_id)
        SourceImport.objects.filter(pk=source_import_id).update(
            staging_status=SourceImport.StagingStatus.FAILED,
            staging_error=str(exc),
        )
        raise

    return 'staging complete'


@shared_task(name='registry.process_source_import')
def process_source_import(source_import_id):
    with transaction.atomic():
        source_import = (
            SourceImport.objects
            .select_for_update()
            .get(pk=source_import_id)
        )
        if source_import.import_status == SourceImport.ImportStatus.SUCCESS:
            return 'already completed'
        if source_import.import_status == SourceImport.ImportStatus.PROCESSING:
            return 'already processing'

        file_path = source_import.file_path
        source_import.import_status = SourceImport.ImportStatus.PROCESSING
        source_import.progress_message = 'Reading and validating the import rows.'
        source_import.error_log = ''
        source_import.save(
            update_fields=[
                'import_status',
                'progress_message',
                'error_log',
            ]
        )

    try:
        if not file_path:
            raise ValueError(f'Source import {source_import_id} has no stored file path.')
        staged_rows = generate_staged_preview(
            file_path,
            source_import.file_encoding,
            source_import.column_mapping,
        )
        SourceImport.objects.filter(pk=source_import_id).update(
            progress_message=(
                f'Prepared {len(staged_rows):,} rows. Saving records and '
                'creating search tasks.'
            )
        )
        commit_parsed_import(
            file_hash=source_import.file_hash,
            file_path=file_path,
            original_filename=source_import.original_filename,
            staged_rows=staged_rows,
            mapping=source_import.column_mapping,
            group_name=(
                source_import.chemical_group.name
                if source_import.chemical_group_id
                else None
            ),
            source_import=source_import,
        )
        SourceImport.objects.filter(pk=source_import_id).update(
            progress_message='Import completed successfully.'
        )
    except Exception as exc:
        logger.exception('Source import %s failed', source_import_id)
        SourceImport.objects.filter(pk=source_import_id).exclude(
            import_status=SourceImport.ImportStatus.SUCCESS,
        ).update(
            import_status=SourceImport.ImportStatus.FAILED,
            progress_message='The import failed.',
            error_log=str(exc),
        )
        raise

    return 'import completed'


@shared_task(bind=True, name='registry.process_data_gathering_run')
def process_data_gathering_run(self, run_id, batch_number=0):
    run = DataGatheringRun.objects.get(pk=run_id)
    now = timezone.now()

    if run.status in (
        DataGatheringRun.Status.PAUSED,
        DataGatheringRun.Status.COMPLETED,
        DataGatheringRun.Status.FAILED,
        DataGatheringRun.Status.CANCELLED,
    ):
        return run.status

    # Start at the requested time, without tying up a worker while waiting.
    if run.scheduled_for > now:
        process_data_gathering_run.apply_async(
            args=[str(run.pk), batch_number],
            eta=run.scheduled_for,
        )
        return 'scheduled'

    with transaction.atomic():
        run = DataGatheringRun.objects.select_for_update().get(pk=run_id)

        if run.status in (
            DataGatheringRun.Status.PAUSED,
            DataGatheringRun.Status.COMPLETED,
            DataGatheringRun.Status.FAILED,
            DataGatheringRun.Status.CANCELLED,
        ):
            return run.status

        if run.status == DataGatheringRun.Status.SCHEDULED:
            run.status = DataGatheringRun.Status.RUNNING
            run.started_at = now
            run.save(update_fields=['status', 'started_at'])

    run.refresh_from_db()
    if run.status in (
        DataGatheringRun.Status.PAUSED,
        DataGatheringRun.Status.COMPLETED,
        DataGatheringRun.Status.FAILED,
        DataGatheringRun.Status.CANCELLED,
    ):
        return run.status

    deadline = run.started_at + timedelta(minutes=run.max_runtime_minutes)
    if now >= deadline:
        updated = DataGatheringRun.objects.filter(
            pk=run.pk,
            status=DataGatheringRun.Status.RUNNING,
        ).update(
            status=DataGatheringRun.Status.PAUSED,
            finished_at=now,
            pause_reason='Run time limit reached.',
        )
        if updated:
            return 'time limit reached'
        return DataGatheringRun.objects.get(pk=run.pk).status

    remaining_tasks = None
    if run.max_tasks is not None:
        remaining_tasks = run.max_tasks - run.processed_count
        if remaining_tasks <= 0:
            updated = DataGatheringRun.objects.filter(
                pk=run.pk,
                status=DataGatheringRun.Status.RUNNING,
            ).update(
                status=DataGatheringRun.Status.COMPLETED,
                finished_at=timezone.now(),
                pause_reason=f'Maximum task limit ({run.max_tasks}) reached.',
            )
            if updated:
                return 'task limit reached'
            return DataGatheringRun.objects.get(pk=run.pk).status

    batch_size = run.batch_size
    if remaining_tasks is not None:
        batch_size = min(batch_size, remaining_tasks)

    lock_key = f"provider-batch:{run.provider_code.lower()}"
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_try_advisory_lock(hashtext(%s))',
            [lock_key],
        )
        lock_acquired = cursor.fetchone()[0]

    if not lock_acquired:
        raise self.retry(countdown=30, max_retries=None)

    retry_delay = None

    try:
        task_log, claimed_count, has_more = run_provider_batch(
            provider_code=run.provider_code,
            batch_size=batch_size,
            force_chemspider_small_batches=run.force_chemspider_small_batches,
            run_id=run.pk,
        )

    except ProviderRateLimited as exc:
        with transaction.atomic():
            run = DataGatheringRun.objects.select_for_update().get(pk=run_id)

            if run.status in (
                DataGatheringRun.Status.PAUSED,
                DataGatheringRun.Status.CANCELLED,
            ):
                return (
                    'cancelled'
                    if run.status == DataGatheringRun.Status.CANCELLED
                    else 'already paused'
                )

            run.rate_limit_retries += 1
            retry_delay = rate_limit_backoff_seconds(
                run.rate_limit_retries,
                exc.retry_after_seconds,
            )
            run.pause_reason = (
                f'Provider rate limited the request; retrying after '
                f'{retry_delay} seconds (attempt {run.rate_limit_retries}).'
            )
            run.save(update_fields=['rate_limit_retries', 'pause_reason'])

    except ProviderAccessDenied as exc:
        with transaction.atomic():
            run = DataGatheringRun.objects.select_for_update().get(pk=run_id)
            if run.status == DataGatheringRun.Status.CANCELLED:
                return 'cancelled after current batch'
            run.status = DataGatheringRun.Status.PAUSED
            run.finished_at = timezone.now()
            run.pause_reason = str(exc)
            run.save(update_fields=['status', 'finished_at', 'pause_reason'])
        return 'paused after provider access denied'

    except Exception as exc:
        logger.exception(
            "Data-gathering batch failed for provider %s",
            run.provider_code,
        )
        run.refresh_from_db()
        if run.status in (
            DataGatheringRun.Status.PAUSED,
            DataGatheringRun.Status.CANCELLED,
        ):
            return f'{run.status.lower()} after current batch failure'

        now = timezone.now()
        run.status = DataGatheringRun.Status.FAILED
        run.finished_at = now
        run.pause_reason = f'Provider batch failed: {exc}'[:2000]
        run.save(update_fields=['status', 'finished_at', 'pause_reason'])
        raise

    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT pg_advisory_unlock(hashtext(%s))',
                [lock_key],
            )
        connection.close()

    if retry_delay is not None:
        process_data_gathering_run.apply_async(
            args=[str(run_id), batch_number],
            countdown=retry_delay,
        )
        return f'rate limited; retry queued after {retry_delay}s'

    run.refresh_from_db()
    run.processed_count += task_log.processed_items
    run.rate_limit_retries = 0
    now = timezone.now()

    if run.status == DataGatheringRun.Status.CANCELLED:
        run.save(update_fields=['processed_count', 'rate_limit_retries'])
        return 'cancelled after current batch'

    if run.status == DataGatheringRun.Status.PAUSED:
        run.save(update_fields=['processed_count', 'rate_limit_retries'])
        return 'paused after current batch'

    if claimed_count and not task_log.processed_items:
        run.status = DataGatheringRun.Status.PAUSED
        run.finished_at = now
        if run.provider_code.lower() == 'chemspider':
            run.pause_reason = (
                'ChemSpider detail lookups require at least 50 ChemSpider ID tasks '
                'in one batch. The selected tasks remain pending. Start a new run '
                'with the small-batch override enabled to force this detail '
                'request, or wait until at least 50 IDs can be selected together. '
                'Resuming this run will not change its override setting.'
            )
        else:
            run.pause_reason = (
                'The provider deferred the selected tasks without processing them. '
                'Review its batch threshold before resuming.'
            )
        run.save(
            update_fields=[
                'processed_count',
                'rate_limit_retries',
                'status',
                'finished_at',
                'pause_reason',
            ]
        )
        return 'paused because the provider deferred the batch'

    limit_reached = (
        run.max_tasks is not None
        and run.processed_count >= run.max_tasks
    )

    if limit_reached:
        final_status = DataGatheringRun.Status.COMPLETED
        final_reason = f'Maximum task limit ({run.max_tasks}) reached.'
    elif not has_more:
        final_status = DataGatheringRun.Status.COMPLETED
        final_reason = ''
    elif now >= deadline:
        final_status = DataGatheringRun.Status.PAUSED
        final_reason = 'Run time limit reached.'
    else:
        run.save(update_fields=['processed_count', 'rate_limit_retries'])
        delay = getattr(settings, 'DATA_GATHERING_BATCH_PAUSE_SECONDS', 30)
        process_data_gathering_run.apply_async(
            args=[str(run.pk), batch_number + 1],
            countdown=delay,
        )
        return 'next batch queued'

    updated = DataGatheringRun.objects.filter(
        pk=run.pk,
        status=DataGatheringRun.Status.RUNNING,
    ).update(
        processed_count=run.processed_count,
        rate_limit_retries=run.rate_limit_retries,
        status=final_status,
        finished_at=now,
        pause_reason=final_reason,
    )
    if updated:
        return final_status

    run.refresh_from_db()
    if run.status in (
        DataGatheringRun.Status.PAUSED,
        DataGatheringRun.Status.CANCELLED,
    ):
        run.processed_count += task_log.processed_items
        run.save(update_fields=['processed_count'])
    return run.status


@shared_task(name='registry.dispatch_due_data_gathering_runs')
def dispatch_due_data_gathering_runs():
    now = timezone.now()

    with transaction.atomic():
        due_runs = list(
            DataGatheringRun.objects
            .select_for_update(skip_locked=True)
            .filter(
                status=DataGatheringRun.Status.SCHEDULED,
                scheduled_for__lte=now,
                dispatched_at__isnull=True,
            )
            .order_by('scheduled_for')[:50]
        )

        run_ids = [run.pk for run in due_runs]
        if run_ids:
            DataGatheringRun.objects.filter(pk__in=run_ids).update(
                dispatched_at=now
            )

    dispatched = 0

    for run_id in run_ids:
        try:
            process_data_gathering_run.apply_async(
                args=[str(run_id)],
                queue='registry',
            )
        except Exception:
            logger.exception(
                "Could not dispatch data-gathering run %s",
                run_id,
            )
            DataGatheringRun.objects.filter(
                pk=run_id,
                status=DataGatheringRun.Status.SCHEDULED,
            ).update(dispatched_at=None)
        else:
            dispatched += 1

    return dispatched


@shared_task(name="registry.build_structure_table_preview")
def build_structure_table_preview(run_id):
    process_next_preview_chunk(run_id)

    if scan_has_more_findings(run_id):
        build_structure_table_preview.apply_async(
            args=[run_id],
            queue="registry",
            countdown=1,
        )
    else:
        finalize_preview_reports(run_id)
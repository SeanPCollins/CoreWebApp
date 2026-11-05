from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.registry.models import DataGatheringRun, ProviderTaskLog, SearchTask


def cancel_provider_run(run_id, reason):
    reason = reason.strip()
    if not reason:
        raise ValidationError('Enter a reason for cancelling this run.')
    if len(reason) > 500:
        raise ValidationError('Cancellation reason must be 500 characters or fewer.')

    with transaction.atomic():
        run = DataGatheringRun.objects.select_for_update().get(pk=run_id)
        if run.status not in (
            DataGatheringRun.Status.SCHEDULED,
            DataGatheringRun.Status.RUNNING,
            DataGatheringRun.Status.PAUSED,
        ):
            raise ValidationError(
                f'This {run.provider_code} run is already {run.get_status_display().lower()}.'
            )

        now = timezone.now()
        cancellation_summary = f'Cancelled by operator: {reason}'
        run.status = DataGatheringRun.Status.CANCELLED
        run.finished_at = now
        run.pause_reason = cancellation_summary
        run.save(update_fields=['status', 'finished_at', 'pause_reason'])

        cancelled_logs = ProviderTaskLog.objects.filter(
            provider__iexact=run.provider_code,
            status='RUNNING',
        ).update(
            status='CANCELLED',
            completed_at=now,
            log_summary=cancellation_summary,
        )
        requeued_tasks = SearchTask.objects.filter(
            web_source__provider_code__iexact=run.provider_code,
            status=SearchTask.TaskStatus.PROCESSING,
        ).update(
            status=SearchTask.TaskStatus.PENDING,
            is_completed=False,
            error_message='',
        )

    return run, cancelled_logs, requeued_tasks


def dismiss_provider_run(run_id):
    with transaction.atomic():
        run = DataGatheringRun.objects.select_for_update().get(
            pk=run_id,
        )
        if run.status not in (
            DataGatheringRun.Status.FAILED,
            DataGatheringRun.Status.CANCELLED,
        ):
            raise ValidationError(
                'Only failed or cancelled runs can be dismissed.'
            )
        run.is_dismissed = True
        run.save(update_fields=['is_dismissed'])
        return run

import abc
import time
import logging
import requests
from django.utils import timezone
from apps.registry.models import ProviderTaskLog, WebFinding, SearchTask
from datetime import datetime, timezone as datetime_timezone
from email.utils import parsedate_to_datetime
import math
import sys

logger = logging.getLogger(__name__)

class BaseProvider(abc.ABC):
    """
    Abstract Base Provider enforcing a unified contract, session handling, 
    rate-limiting, and error-handling lifecycle across all chemical data sources.
    """
    provider_code = "base"
    min_request_interval = 0.3
    default_retry_after_seconds = 60

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._last_request_time = 0.0
        self.session = requests.Session()
        self.session.headers.update(self.get_default_headers())
        self.session.hooks.setdefault('response', []).append(
            self._raise_for_provider_limit
        )

    def get_default_headers(self):
        return {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
        }

    def polite_throttle(self):
        """Ensures compliance with external API rate limits."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self._last_request_time = time.time()

    def save_progress_checkpoint(self, task_log, interval=5):
        if task_log.processed_items and task_log.processed_items % interval == 0:
            task_log.save(
                update_fields=[
                    'processed_items',
                    'success_count',
                    'not_found_count',
                    'error_count',
                ]
            )

    def cancellation_requested(self, task_log):
        return ProviderTaskLog.objects.filter(
            pk=task_log.pk,
            status='CANCELLED',
        ).exists()

    @abc.abstractmethod
    def execute_batch(self, tasks, task_log, *args, **kwargs):
        """Concrete providers must implement their specific query and parsing cycle here."""
        pass

    def handle_task_success(self, task, url, payload, msg, task_log):
        """Standardized helper for recording successful findings."""
        task.status = SearchTask.TaskStatus.SUCCESS_FOUND
        task.is_completed = True
        if hasattr(task, 'completed_at'):
            task.completed_at = timezone.now()
        task.error_message = msg
        task.save()

        WebFinding.objects.update_or_create(
            search_task=task,
            defaults={
                'source_url': url,
                'raw_payload': payload,
                'parsed_successfully': True
            }
        )
        task_log.success_count += 1

    def handle_task_not_found(self, task, url, msg, task_log):
        """Standardized helper for 404 / missing records."""
        task.status = SearchTask.TaskStatus.NOT_FOUND
        task.is_completed = True
        task.error_message = msg
        task.save()

        WebFinding.objects.update_or_create(
            search_task=task,
            defaults={
                'source_url': url,
                'raw_payload': msg,
                'parsed_successfully': False
            }
        )
        task_log.not_found_count += 1

    def handle_task_error(self, task, url, err_msg, task_log):
        active_exception = sys.exc_info()[1]

        if isinstance(active_exception, (ProviderRateLimited, ProviderAccessDenied)):
            raise active_exception

        if active_exception is not None:
            self._raise_for_provider_exception(active_exception)

        # Keep the existing ordinary error handling below this point.
        task.run_count += 1
        if task.run_count >= 3:
            task.status = SearchTask.TaskStatus.ERROR
            task.is_completed = True
            task.error_message = f"Retries exhausted (3). Failed with: {err_msg}"
            task_log.error_count += 1
        else:
            task.status = SearchTask.TaskStatus.ERROR
            task.is_completed = False
            task.error_message = err_msg
            task_log.error_count += 1

        task.save()
        WebFinding.objects.update_or_create(
            search_task=task,
            defaults={
                'source_url': url,
                'raw_payload': err_msg,
                'parsed_successfully': False
            }
        )

    def _raise_for_provider_exception(self, exception):
        response = getattr(exception, 'response', None)

        status_code = getattr(response, 'status_code', None)
        if status_code is None:
            status_code = getattr(exception, 'status_code', None)
        if status_code is None:
            status_code = getattr(exception, 'status', None)

        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            return

        if status_code in (401, 403):
            raise ProviderAccessDenied(
                f"{self.provider_code} returned HTTP {status_code}."
            ) from exception

        if status_code == 429:
            headers = (
                getattr(response, 'headers', None)
                or getattr(exception, 'headers', None)
                or {}
            )
            retry_after = next(
                (
                    value for key, value in headers.items()
                    if key.lower() == 'retry-after'
                ),
                None,
            )
            raise ProviderRateLimited(
                retry_after_seconds(
                    retry_after,
                    default=self.default_retry_after_seconds,
                )
            ) from exception

    def _raise_for_provider_limit(self, response, *args, **kwargs):
        if response.status_code in (401, 403):
            raise ProviderAccessDenied(
                f"{self.provider_code} returned HTTP {response.status_code}."
            )

        if response.status_code == 429:
            delay = retry_after_seconds(
                response.headers.get('Retry-After'),
                default=self.default_retry_after_seconds,
            )
            raise ProviderRateLimited(delay)

        return response

class ProviderRateLimited(Exception):
    def __init__(self, retry_after_seconds):
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(f"Provider rate-limited the request; retry after "
                         f"{self.retry_after_seconds} seconds.")


class ProviderAccessDenied(Exception):
    pass


def retry_after_seconds(value, default=60):
    if not value:
        return default

    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(1, math.ceil(seconds))
    except (TypeError, ValueError):
        pass

    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=datetime_timezone.utc)
        seconds = (
            retry_at - datetime.now(datetime_timezone.utc)
        ).total_seconds()
        if math.isfinite(seconds):
            return max(1, math.ceil(seconds))
    except (TypeError, ValueError, OverflowError):
        pass

    return default
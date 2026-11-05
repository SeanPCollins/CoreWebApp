import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("core_core")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.beat_schedule = {
    'dispatch-due-data-gathering-runs': {
        'task': 'registry.dispatch_due_data_gathering_runs',
        'schedule': 30.0,
        'options': {'queue': 'registry'},
    },
}
app.autodiscover_tasks()
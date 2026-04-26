"""Celery application bootstrap.

The worker and beat process import this module via the ``celery -A playto_pay``
entrypoint. Tasks are autodiscovered from each installed app's ``tasks.py``.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "playto_pay.settings")

app = Celery("playto_pay")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

"""Pytest configuration shared across all test modules.

We enable Celery's eager mode so tests don't need a real broker. Tasks fired
via ``.delay(...)`` execute synchronously inline; ``transaction.on_commit``
hooks still fire after the wrapping transaction commits.
"""

from django.conf import settings


def pytest_configure(config):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True

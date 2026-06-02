import os
import sys


def _should_import_celery_app():
    argv0 = os.path.basename(sys.argv[0] or "").lower()
    if argv0 in ("manage.py", "django-admin", "django-admin.exe", "django-admin.py"):
        return False
    if argv0.startswith("pytest"):
        return False
    if "celery" in argv0:
        return True
    return os.getenv("DJANGO_EAGER_CELERY_IMPORT", "").strip() == "1"


if _should_import_celery_app():
    from .celery import app as celery_app
else:
    celery_app = None

__all__ = ("celery_app",)

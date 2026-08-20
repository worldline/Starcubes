"""WSGI entry point for production servers (gunicorn, uwsgi, ...)."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "starcubes.settings")
application = get_wsgi_application()

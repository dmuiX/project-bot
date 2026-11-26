"""Production WSGI entrypoint for Project Bot (Gunicorn).

Run with a single worker process so the integrated APScheduler runs once:
    gunicorn --workers 1 --threads 4 --bind 0.0.0.0:8002 --timeout 120 wsgi:app
"""

from server_enhanced import app, startup

startup()

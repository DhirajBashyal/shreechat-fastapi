"""Gunicorn configuration file for shreechat-ai-fastapi."""

from app.core.config import settings

worker_class = "uvicorn.workers.UvicornWorker"
wsgi_app = "app.main:app"
bind = settings.GUNICORN_BIND

accesslog = settings.GUNICORN_ACCESS_LOG
errorlog = settings.GUNICORN_ERROR_LOG
workers = settings.GUNICORN_WORKERS
threads = settings.GUNICORN_THREADS
capture_output = True
loglevel = "info"

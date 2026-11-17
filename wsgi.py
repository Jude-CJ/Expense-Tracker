import os

if os.environ.get("APP_ENV", "").lower() != "production":
    raise RuntimeError("Set APP_ENV=production before starting the production WSGI server.")

from app import app

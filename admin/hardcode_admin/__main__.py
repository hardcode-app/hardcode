"""
``python -m hardcode_admin``: the development server. Production runs the app
under gunicorn instead.
"""

import os

from .wsgi import app

app.run(
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8080")),
    threaded=True,
)

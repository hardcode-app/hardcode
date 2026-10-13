# HardCode admin: the Kubernetes operator and admin web UI (Python, Flask and
# gunicorn from the Ubuntu 26.04 archive). Built from the repository root:
#   docker build -f images/admin/Dockerfile -t hardcode-admin .
ARG UBUNTU_VERSION=26.04
FROM ubuntu:${UBUNTU_VERSION}

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-flask gunicorn python3-qrcode openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && userdel --remove ubuntu \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin hardcode

WORKDIR /app
# The CLI package is imported for the allowlist, blocked-traffic and language
# code; the admin package is the operator and the UI.
COPY cli/hardcode_cli ./hardcode_cli
COPY admin/hardcode_admin ./hardcode_admin
# Shared with the desktop app: the stylesheet and the icon; and the template
# every new profile starts from.
COPY app/src/styles.css ./hardcode_admin/web/base.css
COPY assets/hardcode-128.png ./hardcode_admin/web/icon.png
COPY template/allowlist.txt ./template-allowlist.txt

USER hardcode
ENV PORT=8080 PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 8080
# One worker: the operator runs inside it; threads serve the UI.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--access-logfile", "-", "hardcode_admin.wsgi:app"]

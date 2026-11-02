# HardCode admin: the Kubernetes operator and admin web UI. Python from the
# Ubuntu 26.04 archive; Flask, gunicorn and qrcode from PyPI, as
# images/admin/uv.lock locks them by hash, and none of the archive's Python
# libraries beside them. Built from the repository root:
#   docker build -f images/admin/Dockerfile -t hardcode-admin .
# Ubuntu 26.04 (resolute), one dated build of it, by digest: Dependabot moves
# it to the next dated build once that is a week old (docs/development.md,
# "Dependencies").
FROM ubuntu:resolute-20261006@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944 AS packages

# uv, as the build image has it (images/build/uv/requirements.txt, every file
# checked against its hash by the archive's pip); then the Python libraries
# into /opt/venv, as uv.lock locks them: every file checked against its hash,
# wheels only, so no package's build script runs, on the archive's Python.
# Neither uv nor pip is in the image this stage builds for.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
COPY images/build/uv/requirements.txt /tmp/uv-requirements.txt
RUN python3 -m venv /opt/uv \
 && /opt/uv/bin/pip install \
      --no-cache-dir \
      --require-hashes \
      --only-binary :all: \
      -r /tmp/uv-requirements.txt
COPY images/admin/pyproject.toml images/admin/uv.lock /tmp/admin-python/
RUN UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never /opt/uv/bin/uv sync \
      --project /tmp/admin-python \
      --locked \
      --no-build \
      --no-cache \
      --python /usr/bin/python3

FROM ubuntu:resolute-20261006@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && userdel --remove ubuntu \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin hardcode

# The venv runs on this stage's python3, the same archive package.
COPY --from=packages /opt/venv /opt/venv

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
ENV PATH="/opt/venv/bin:$PATH" PORT=8080 PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 8080
# One worker: the operator runs inside it; threads serve the UI.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--access-logfile", "-", "hardcode_admin.wsgi:app"]

"""
GET a URL over HTTPS with Python's default TLS settings (the system's CA
bundle, or SSL_CERT_FILE) and print the status, or the error. The Debian
and distroless containers of tests/docker.py run it.

    python3 fetch.py URL
"""

import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=10) as response:
        print(f"status {response.status}")
except OSError as error:
    print(f"failed: {error}")
    sys.exit(1)

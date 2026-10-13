"""Opt-in source transport matching the tested system curl invocation.

GET and JSON POST only; curl handles redirects and decompression. No cookie jar,
automatic retries, browser impersonation, or fallback after a rate limit.
Never use this transport for Supabase.
"""
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import requests
from requests.hooks import dispatch_hook
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers

from scrapers.source_http import stop_on_source_rate_limit

LOG = logging.getLogger(__name__)


class SystemCurlSession(requests.Session):
    def __init__(self, headers, min_interval=3):
        super().__init__()
        self.headers.update(headers)
        self.trust_env = False  # curl itself reads its normal proxy environment.
        self.binary = shutil.which('curl')
        if not self.binary:
            raise RuntimeError('System curl with HTTP/2 support is required')
        self.min_interval = min_interval
        self.last_request = None

    def send(self, request, **kwargs):
        if request.method not in ('GET', 'POST') or not request.url.startswith('https://'):
            raise ValueError('System curl source transport accepts HTTPS GET/POST only')
        if kwargs.get('stream') or kwargs.get('verify') is False or kwargs.get('cert'):
            raise ValueError('Unsupported source transport options')
        timeout = kwargs.get('timeout') or 30
        if isinstance(timeout, tuple):
            timeout = sum(timeout)
        timeout = float(timeout)
        if self.last_request is not None:
            time.sleep(max(0, self.min_interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='highlide-curl-') as directory:
            root = Path(directory)
            outgoing, incoming, body = (root / name for name in ('request-headers', 'response-headers', 'body'))
            # Header values may contain public storefront tokens; keep them out
            # of process arguments and logs. The temporary directory is private.
            outgoing.write_text(''.join(f'{k}: {v}\n' for k, v in request.headers.items()))
            command = [self.binary, '-q', '--http2', '--compressed', '--silent', '--show-error',
                       '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '3',
                       '--max-time', str(timeout), '--header', '@' + str(outgoing),
                       '--dump-header', str(incoming), '--output', str(body),
                       '--write-out', '%{http_code}\n%{url_effective}\n%{http_version}']
            if kwargs.get('allow_redirects', True):
                command.append('--location')
            data = request.body
            if request.method == 'POST':
                command.extend(['--data-binary', '@-'])
                data = data or b''
            if isinstance(data, str):
                data = data.encode('utf-8')
            if data is not None and not isinstance(data, bytes):
                raise ValueError('Streaming request bodies are unsupported')
            command.extend(['--url', request.url])
            try:
                process = subprocess.run(command, input=data, capture_output=True, timeout=timeout + 5)
            except subprocess.TimeoutExpired as exc:
                raise requests.Timeout('System curl exceeded source request timeout', request=request) from exc
            if process.returncode:
                error = requests.Timeout if process.returncode == 28 else requests.ConnectionError
                raise error(f'System curl failed (exit {process.returncode})', request=request)
            status, url, version = process.stdout.decode().strip().split('\n')
            headers = CaseInsensitiveDict()
            for line in incoming.read_text(encoding='iso-8859-1').splitlines():
                if line.startswith('HTTP/'):
                    headers.clear()  # Discard proxy CONNECT and redirect headers.
                elif ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip()] = value.strip()
            response = requests.Response()
            response.status_code = int(status)
            response.url = url
            response.headers = headers
            response.encoding = get_encoding_from_headers(headers)
            response.request = request
            response._content = body.read_bytes()
            response._content_consumed = True
            LOG.debug('System curl HTTP/%s status=%s url=%s', version, status, url)
            stop_on_source_rate_limit(response)
            return dispatch_hook('response', request.hooks, response, **kwargs)

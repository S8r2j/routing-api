"""
Request logging middleware.

Logs every inbound request with the client IP, HTTP method, path, user-agent,
and response status code. Throttle violations (429) are promoted to WARNING so
they surface clearly in the log file and can be monitored for abuse patterns.
"""

import logging
import time

logger = logging.getLogger('routing.access')


def _get_client_ip(request) -> str:
    """
    Extract the real client IP, respecting X-Forwarded-For when present.
    Takes the first (leftmost) address in the header which is the originating
    client; subsequent entries are proxies added along the way.
    """
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


class RequestLoggingMiddleware:
    """
    ASGI-compatible middleware that logs each request/response pair.

    Log format:
        {ip} "{method} {path}{query}" {status} {duration_ms}ms UA="{user_agent}"

    429 responses (throttled) are logged at WARNING; all others at INFO.
    This gives operators a clear signal of clients hitting rate limits without
    drowning INFO logs in noise.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)
        duration_ms = (time.monotonic() - start) * 1000

        ip = _get_client_ip(request)
        method = request.method
        path = request.path
        query = f'?{request.META["QUERY_STRING"]}' if request.META.get('QUERY_STRING') else ''
        status = response.status_code
        ua = request.META.get('HTTP_USER_AGENT', '-')

        msg = f'{ip} "{method} {path}{query}" {status} {duration_ms:.1f}ms UA="{ua}"'

        if status == 429:
            logger.warning('THROTTLED %s', msg)
        else:
            logger.info(msg)

        return response

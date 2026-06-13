import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.views import View

from .exceptions import GeocodingError, NoRouteError, RoutingServiceError
from .serializers import RouteRequestSerializer
from .services.route_service import plan_route

logger = logging.getLogger("routing.views")


class RouteView(View):
    """
    GET /api/v1/route/?start=<location>&end=<location>

    Validates input, checks the route cache, delegates to route_service.plan_route,
    stores the result in cache, and returns a JsonResponse.
    All business logic lives in route_service; all DB logic lives in db.
    """

    async def get(self, request) -> JsonResponse:
        try:
            return await self._handle(request)
        except Exception as exc:
            logger.error("Unhandled exception in RouteView: %s", exc, exc_info=True)
            return JsonResponse(
                {"error": "An unexpected error occurred. Please try again."},
                status=500,
            )

    async def _handle(self, request) -> JsonResponse:
        # 1. Validate input
        serializer = RouteRequestSerializer(data=request.GET)
        if not serializer.is_valid():
            flat_errors = {}
            for field, errors in serializer.errors.items():
                flat_errors[field] = errors[0] if isinstance(errors, list) else str(errors)
            return JsonResponse({"error": flat_errors}, status=400)

        start_str: str = serializer.validated_data["start"]
        end_str: str = serializer.validated_data["end"]

        # 2. Cache check — identical start/end pairs skip the entire pipeline
        cache_key = "route:" + hashlib.sha256(
            f"{start_str.lower()}|{end_str.lower()}".encode()
        ).hexdigest()

        cached = cache.get(cache_key)
        if cached is not None:
            logger.debug("Cache hit for '%s' → '%s'", start_str, end_str)
            return JsonResponse(cached)

        # 3. Delegate to service layer
        try:
            response_data = await plan_route(start_str, end_str)
        except GeocodingError as exc:
            return JsonResponse({"error": str(exc)}, status=422)
        except RoutingServiceError as exc:
            msg = str(exc)
            status = 504 if "timed out" in msg.lower() else 502
            return JsonResponse({"error": msg}, status=status)
        except (NoRouteError, ValueError) as exc:
            return JsonResponse({"error": str(exc)}, status=422)

        # 4. Cache and return
        cache.set(cache_key, response_data, timeout=settings.ROUTE_CACHE_TIMEOUT)
        return JsonResponse(response_data)

class NoRouteError(Exception):
    """No valid fuel stop sequence can cover the route."""
    pass


class GeocodingError(Exception):
    """Location string could not be resolved to coordinates."""
    pass


class RoutingServiceError(Exception):
    """OSRM routing service error."""
    pass

import asyncio
import logging
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Initialise Django before importing anything that touches models or settings.
django_asgi_app = get_asgi_application()

logger = logging.getLogger('routing')


async def lifespan(scope, receive, send):
    """
    ASGI lifespan handler — runs once per Uvicorn worker process.
    Loads the KD-tree in a thread pool so the sync ORM query never
    blocks the async event loop.
    """
    if scope['type'] != 'lifespan':
        await django_asgi_app(scope, receive, send)
        return

    while True:
        event = await receive()

        if event['type'] == 'lifespan.startup':
            try:
                from apps.routing.services.station_index import FuelStationIndex
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, FuelStationIndex.load)
                logger.info("FuelStationIndex loaded via lifespan startup")
            except Exception as exc:
                logger.error("Failed to load FuelStationIndex at startup: %s", exc)
                await send({'type': 'lifespan.startup.failed', 'message': str(exc)})
                return
            await send({'type': 'lifespan.startup.complete'})

        elif event['type'] == 'lifespan.shutdown':
            await send({'type': 'lifespan.shutdown.complete'})
            return


application = lifespan

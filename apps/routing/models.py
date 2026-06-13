from django.db import models


class FuelStation(models.Model):
    """Represents a single fuel station row from the price CSV, geocoded to lat/lon."""

    opis_id = models.IntegerField()
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    lat = models.FloatField()
    lon = models.FloatField()
    price = models.DecimalField(max_digits=10, decimal_places=8)
    rack_id = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["lat", "lon"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) - ${self.price}"

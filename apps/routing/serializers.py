from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        required=True,
        max_length=200,
        error_messages={'required': 'start query parameter is required'},
    )
    end = serializers.CharField(
        required=True,
        max_length=200,
        error_messages={'required': 'end query parameter is required'},
    )

    def validate_start(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("start cannot be blank")
        if len(value) < 3:
            raise serializers.ValidationError("start location too short to be valid")
        return value

    def validate_end(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("end cannot be blank")
        if len(value) < 3:
            raise serializers.ValidationError("end location too short to be valid")
        return value

    def validate(self, data: dict) -> dict:
        if data.get('start', '').lower() == data.get('end', '').lower():
            raise serializers.ValidationError(
                "start and end cannot be the same location"
            )
        return data

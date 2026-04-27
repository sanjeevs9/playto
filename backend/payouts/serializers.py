"""DRF serializers for Payout endpoints."""

from rest_framework import serializers

from .models import Payout


class PayoutCreateSerializer(serializers.Serializer):
    """Validates the POST /api/v1/payouts request body.

    Note: amount validation here is shape-only ("positive integer"). Funds
    sufficiency is checked inside the locked transaction in
    ``payouts.services.create_payout`` — that's the only safe place because
    only the locked check sees the latest committed balance.
    """

    amount_paise = serializers.IntegerField(min_value=1)
    bank_account_id = serializers.IntegerField(min_value=1)


class PayoutSerializer(serializers.ModelSerializer):
    """Read-only payout representation returned by GET endpoints."""

    id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Payout
        fields = [
            "id",
            "merchant_id",
            "bank_account_id",
            "amount_paise",
            "status",
            "retry_count",
            "failure_reason",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

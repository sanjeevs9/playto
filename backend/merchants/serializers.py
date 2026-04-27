"""DRF serializers for merchant-side read endpoints.

These power the React dashboard. All endpoints are read-only — the only
balance-mutating endpoint in the system is ``POST /api/v1/payouts``.
"""

from rest_framework import serializers

from .models import BankAccount, LedgerEntry, Merchant


class MerchantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Merchant
        fields = ["id", "name", "email", "created_at"]
        read_only_fields = fields


class BankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankAccount
        fields = [
            "id",
            "holder_name",
            "account_number_last4",
            "ifsc",
            "nickname",
            "is_default",
        ]
        read_only_fields = fields


class LedgerEntrySerializer(serializers.ModelSerializer):
    related_payout_id = serializers.SerializerMethodField()

    class Meta:
        model = LedgerEntry
        fields = [
            "id",
            "amount_paise",
            "entry_type",
            "description",
            "related_payout_id",
            "created_at",
        ]
        read_only_fields = fields

    def get_related_payout_id(self, obj: LedgerEntry) -> str | None:
        return str(obj.related_payout_id) if obj.related_payout_id else None

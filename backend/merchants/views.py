"""Read-only merchant endpoints for the dashboard.

No auth — the take-home does not require it. Production would scope these to
the authenticated merchant via session or API key.
"""

from django.db.models import Sum
from rest_framework import views
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from .models import BankAccount, LedgerEntry, Merchant
from .serializers import (
    BankAccountSerializer,
    LedgerEntrySerializer,
    MerchantSerializer,
)
from .services import available_balance_paise


def _get_merchant(pk: int) -> Merchant:
    try:
        return Merchant.objects.get(id=pk)
    except Merchant.DoesNotExist as exc:
        raise NotFound("merchant not found") from exc


class MerchantsView(views.APIView):
    """GET /api/v1/merchants — list all merchants for the dashboard selector."""

    def get(self, request):
        qs = Merchant.objects.all().order_by("id")
        return Response({"results": MerchantSerializer(qs, many=True).data})


class MerchantBalanceView(views.APIView):
    """GET /api/v1/merchants/<id>/balance — current available + held balance.

    available_paise = SUM(LedgerEntry.amount_paise) — derived, not stored.
    held_paise      = SUM(Payout.amount_paise) WHERE status IN (PENDING,
                      PROCESSING) — informational; the held funds are
                      already reflected as DEBIT entries on the ledger so
                      available_paise does not double-count them.
    """

    def get(self, request, pk: int):
        merchant = _get_merchant(pk)

        # Lazy import to avoid circular merchants <-> payouts at module load.
        from payouts.models import Payout

        held = (
            Payout.objects.filter(
                merchant=merchant,
                status__in=[Payout.Status.PENDING, Payout.Status.PROCESSING],
            ).aggregate(total=Sum("amount_paise"))["total"]
            or 0
        )

        return Response(
            {
                "merchant_id": merchant.id,
                "available_paise": available_balance_paise(merchant.id),
                "held_paise": held,
            }
        )


class MerchantLedgerView(views.APIView):
    """GET /api/v1/merchants/<id>/ledger — recent ledger entries."""

    PAGE_SIZE = 50

    def get(self, request, pk: int):
        merchant = _get_merchant(pk)
        entries = LedgerEntry.objects.filter(merchant=merchant).order_by(
            "-created_at"
        )[: self.PAGE_SIZE]
        return Response(
            {"results": LedgerEntrySerializer(entries, many=True).data}
        )


class MerchantBankAccountsView(views.APIView):
    """GET /api/v1/merchants/<id>/bank-accounts — bank accounts for the form."""

    def get(self, request, pk: int):
        merchant = _get_merchant(pk)
        banks = BankAccount.objects.filter(merchant=merchant).order_by(
            "-is_default", "id"
        )
        return Response(
            {"results": BankAccountSerializer(banks, many=True).data}
        )

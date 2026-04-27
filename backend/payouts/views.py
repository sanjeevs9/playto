"""HTTP layer for the payout API.

Notes on auth:
    The take-home does not require auth. We identify the calling merchant
    via the ``X-Merchant-Id`` header. In production this would be an
    authenticated session / API key tied to a Merchant; the API contract
    everywhere else stays the same.
"""

import uuid

from rest_framework import status, views
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from .models import Payout
from .serializers import PayoutCreateSerializer, PayoutSerializer
from .services import IdempotencyConflict, create_payout


def _require_merchant_id(request) -> int:
    raw = request.headers.get("X-Merchant-Id")
    if not raw:
        raise ValidationError({"merchant": "X-Merchant-Id header is required"})
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationError(
            {"merchant": "X-Merchant-Id must be an integer"}
        ) from exc


class PayoutsView(views.APIView):
    """POST /api/v1/payouts — idempotent payout creation.
    GET /api/v1/payouts — list this merchant's payouts."""

    def post(self, request):
        merchant_id = _require_merchant_id(request)

        raw_key = request.headers.get("Idempotency-Key")
        if not raw_key:
            raise ValidationError(
                {"idempotency_key": "Idempotency-Key header is required"}
            )
        try:
            key = uuid.UUID(raw_key)
        except ValueError as exc:
            raise ValidationError(
                {"idempotency_key": "must be a valid UUID"}
            ) from exc

        serializer = PayoutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            body, http_status = create_payout(
                merchant_id=merchant_id,
                idempotency_key=key,
                **serializer.validated_data,
            )
        except IdempotencyConflict as exc:
            return Response(
                {
                    "error": "idempotency_conflict",
                    "detail": str(exc),
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return Response(body, status=http_status)

    def get(self, request):
        merchant_id = _require_merchant_id(request)
        # Page size is intentionally small for the dashboard; pagination is
        # offset/limit if we need to expose more later.
        qs = Payout.objects.filter(merchant_id=merchant_id).order_by("-created_at")[
            :50
        ]
        return Response({"results": PayoutSerializer(qs, many=True).data})


class PayoutDetailView(views.APIView):
    """GET /api/v1/payouts/<uuid> — single payout, scoped to merchant."""

    def get(self, request, pk):
        merchant_id = _require_merchant_id(request)
        try:
            payout = Payout.objects.get(id=pk, merchant_id=merchant_id)
        except Payout.DoesNotExist as exc:
            raise NotFound("payout not found") from exc
        return Response(PayoutSerializer(payout).data)

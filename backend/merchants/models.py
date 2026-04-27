"""Merchant ledger primitives.

The ledger is the single source of truth for merchant balance:

    available_balance_paise(merchant_id) == SUM(LedgerEntry.amount_paise)

Entries are immutable. We never mutate an existing row to "fix" a balance — we
write a compensating entry. That makes the audit trail trivial and keeps the
take-home's invariant ("sum of credits minus debits == displayed balance") true
by definition rather than by application discipline.
"""

from django.db import models
from django.db.models import CheckConstraint, Q


class Merchant(models.Model):
    """A merchant collecting USD from international customers, paid out in INR."""

    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=200)
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "merchants"

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"


class BankAccount(models.Model):
    """Indian bank account where a merchant receives payouts."""

    id = models.BigAutoField(primary_key=True)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.CASCADE, related_name="bank_accounts"
    )
    holder_name = models.CharField(max_length=200)
    # Last 4 digits only — we never store full account numbers in this demo.
    account_number_last4 = models.CharField(max_length=4)
    ifsc = models.CharField(max_length=11)
    nickname = models.CharField(max_length=100, blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bank_accounts"
        indexes = [models.Index(fields=["merchant", "is_default"])]

    def __str__(self) -> str:
        return f"{self.holder_name} (•••• {self.account_number_last4})"


class LedgerEntry(models.Model):
    """Immutable balance-changing entry.

    Sign convention:
        CREDIT  -> amount_paise > 0 (customer payment)
        REFUND  -> amount_paise > 0 (failed payout returning funds)
        DEBIT   -> amount_paise < 0 (payout reserving / settling funds)

    Balance = SUM(amount_paise). Computed via Postgres aggregation, not Python.
    """

    class EntryType(models.TextChoices):
        CREDIT = "CREDIT", "Customer payment credit"
        DEBIT = "DEBIT", "Payout debit"
        REFUND = "REFUND", "Refund for failed payout"

    id = models.BigAutoField(primary_key=True)
    merchant = models.ForeignKey(
        Merchant, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    amount_paise = models.BigIntegerField()
    entry_type = models.CharField(max_length=16, choices=EntryType.choices)
    related_payout = models.ForeignKey(
        # Forward string reference — Payout lives in the payouts app and we
        # want LedgerEntry definable without importing that module.
        "payouts.Payout",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ledger_entries"
        indexes = [models.Index(fields=["merchant", "-created_at"])]
        constraints = [
            # No zero-amount entries — those would be silent no-ops in the ledger.
            CheckConstraint(
                condition=~Q(amount_paise=0),
                name="ledger_amount_nonzero",
            ),
            # Sign must match type. Defends against application bugs that try
            # to write a CREDIT with a negative amount or a DEBIT with a
            # positive amount; the database refuses the row.
            CheckConstraint(
                condition=(
                    (Q(entry_type="CREDIT") & Q(amount_paise__gt=0))
                    | (Q(entry_type="REFUND") & Q(amount_paise__gt=0))
                    | (Q(entry_type="DEBIT") & Q(amount_paise__lt=0))
                ),
                name="ledger_sign_matches_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entry_type} {self.amount_paise} paise"

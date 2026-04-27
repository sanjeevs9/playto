"""Concurrency test for the lock-and-debit primitive.

Two threads simultaneously attempt to debit a merchant whose balance covers
exactly one of the two debits. ``SELECT ... FOR UPDATE`` on the merchant row
must serialize the critical section so that exactly one debit succeeds and
the other raises InsufficientFundsError. The final balance, recomputed via
``SUM(amount_paise)`` after the threads join, must equal the seeded balance
minus the single successful debit — proving no double-spend occurred.

This is the single most important test in the take-home: race on a balance,
verify exactly-one-wins, verify the ledger invariant survives.
"""

import threading

import pytest
from django.db import connections
from django.db.models import Sum

from merchants.models import LedgerEntry, Merchant
from merchants.services import InsufficientFundsError, hold_funds


@pytest.mark.django_db(transaction=True)
def test_concurrent_debits_cannot_overdraw():
    merchant = Merchant.objects.create(
        name="Test Merchant", email="concurrency-test@example.com"
    )
    LedgerEntry.objects.create(
        merchant=merchant,
        amount_paise=10_000,  # 100 rupees
        entry_type=LedgerEntry.EntryType.CREDIT,
        description="seed credit",
    )

    successes: list[int] = []
    failures: list[int] = []
    barrier = threading.Barrier(2)
    each_amount_paise = 6_000  # two of these would overdraw the 10_000 balance

    def attempt():
        # Wait until both threads are at the gate, then race.
        barrier.wait()
        try:
            entry = hold_funds(
                merchant_id=merchant.id,
                amount_paise=each_amount_paise,
                description="concurrent test",
            )
            successes.append(entry.id)
        except InsufficientFundsError:
            failures.append(1)
        finally:
            # Each thread gets its own DB connection from Django's per-thread
            # pool. Close it so the test DB doesn't leak open connections.
            connections.close_all()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == 1, (
        f"expected exactly one success, got successes={successes} failures={failures}"
    )
    assert len(failures) == 1, (
        f"expected exactly one failure, got successes={successes} failures={failures}"
    )

    # Final balance: 100 - 60 = 40 rupees = 4000 paise. Computed via DB Sum.
    final_paise = LedgerEntry.objects.filter(merchant=merchant).aggregate(
        total=Sum("amount_paise"),
    )["total"]
    assert final_paise == 4_000

    # Ledger has exactly two rows: the seed credit + the one successful debit.
    assert LedgerEntry.objects.filter(merchant=merchant).count() == 2

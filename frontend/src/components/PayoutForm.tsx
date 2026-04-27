import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  type CreatePayoutResult,
  type PayoutCreateError,
  createPayout,
  fetchBankAccounts,
} from "../api";
import { formatPaise } from "../lib/format";

export function PayoutForm({ merchantId }: { merchantId: number }) {
  const queryClient = useQueryClient();
  const banks = useQuery({
    queryKey: ["bank-accounts", merchantId],
    queryFn: () => fetchBankAccounts(merchantId),
  });

  const [amountRupees, setAmountRupees] = useState<string>("");
  const [bankAccountId, setBankAccountId] = useState<number | "">("");
  const [lastResult, setLastResult] = useState<CreatePayoutResult | null>(null);

  const mutation = useMutation({
    mutationFn: () => {
      const paise = Math.round(parseFloat(amountRupees) * 100);
      return createPayout(merchantId, {
        amount_paise: paise,
        bank_account_id: Number(bankAccountId),
      });
    },
    onSuccess: (result) => {
      setLastResult(result);
      // Refresh dashboard widgets after submit. React Query's invalidate
      // schedules a refetch; the polling interval would catch it eventually
      // anyway, this just makes the UI snap immediately.
      queryClient.invalidateQueries({ queryKey: ["balance", merchantId] });
      queryClient.invalidateQueries({ queryKey: ["ledger", merchantId] });
      queryClient.invalidateQueries({ queryKey: ["payouts", merchantId] });
      if (result.ok) {
        setAmountRupees("");
      }
    },
  });

  const submitDisabled =
    !amountRupees ||
    !bankAccountId ||
    parseFloat(amountRupees) <= 0 ||
    mutation.isPending;

  return (
    <section className="bg-white rounded-xl border border-slate-200 shadow-sm">
      <header className="px-5 py-3 border-b border-slate-100">
        <h2 className="text-sm font-semibold text-slate-700">
          Request a payout
        </h2>
        <p className="text-xs text-slate-500">
          Idempotency-Key (UUID v4) is generated client-side per submit.
        </p>
      </header>

      <form
        className="p-5 space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        <Field label="Amount (₹)">
          <input
            type="number"
            step="0.01"
            min="0.01"
            value={amountRupees}
            onChange={(e) => setAmountRupees(e.target.value)}
            placeholder="0.00"
            className="w-full border border-slate-300 rounded-md px-3 py-2 tabular-nums focus:outline-none focus:ring-2 focus:ring-blue-300"
          />
        </Field>

        <Field label="Bank account">
          <select
            value={bankAccountId}
            onChange={(e) => setBankAccountId(Number(e.target.value))}
            className="w-full border border-slate-300 rounded-md px-3 py-2 bg-white focus:outline-none focus:ring-2 focus:ring-blue-300"
          >
            <option value="" disabled>
              {banks.isLoading ? "Loading…" : "Select…"}
            </option>
            {banks.data?.map((b) => (
              <option key={b.id} value={b.id}>
                {b.holder_name} • •••• {b.account_number_last4} ({b.ifsc})
              </option>
            ))}
          </select>
        </Field>

        <button
          type="submit"
          disabled={submitDisabled}
          className="w-full px-4 py-2 rounded-md bg-slate-900 text-white text-sm font-medium hover:bg-slate-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {mutation.isPending ? "Submitting…" : "Request payout"}
        </button>

        {lastResult && <ResultBanner result={lastResult} />}
      </form>
    </section>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="block text-xs font-medium text-slate-600 mb-1">
        {label}
      </span>
      {children}
    </label>
  );
}

function ResultBanner({ result }: { result: CreatePayoutResult }) {
  if (result.ok) {
    return (
      <div className="rounded-md bg-emerald-50 border border-emerald-200 px-3 py-2 text-xs text-emerald-800">
        Payout queued.{" "}
        <span className="font-mono">
          id={"id" in result.data ? result.data.id.slice(0, 8) : "—"}…
        </span>
      </div>
    );
  }
  const err = result.data as PayoutCreateError;
  let message = err.error;
  if (err.error === "insufficient_funds" && err.available_paise != null) {
    message = `Insufficient funds. Available: ${formatPaise(err.available_paise)}, requested: ${formatPaise(err.requested_paise ?? 0)}`;
  } else if (err.detail) {
    message = err.detail;
  }
  return (
    <div className="rounded-md bg-rose-50 border border-rose-200 px-3 py-2 text-xs text-rose-800">
      {message}
    </div>
  );
}

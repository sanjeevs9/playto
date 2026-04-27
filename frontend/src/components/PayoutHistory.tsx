import { useQuery } from "@tanstack/react-query";
import { fetchPayouts } from "../api";
import { formatPaise, formatRelative } from "../lib/format";
import { payoutStatusClasses } from "../lib/status";

export function PayoutHistory({ merchantId }: { merchantId: number }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["payouts", merchantId],
    queryFn: () => fetchPayouts(merchantId),
    // Poll every 3s — short enough to feel live, long enough not to hammer
    // the worker while it processes a 30s simulation.
    refetchInterval: 3000,
  });

  return (
    <section className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
      <header className="px-5 py-3 border-b border-slate-100">
        <h2 className="text-sm font-semibold text-slate-700">Payout history</h2>
        <p className="text-xs text-slate-500">
          Refreshes every 3s while the worker drives state changes.
        </p>
      </header>

      {isLoading ? (
        <div className="p-6 text-sm text-slate-500">Loading…</div>
      ) : error ? (
        <div className="p-6 text-sm text-rose-600">Failed to load payouts.</div>
      ) : !data || data.length === 0 ? (
        <div className="p-6 text-sm text-slate-500">No payouts yet.</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="text-left px-5 py-2 font-medium">Status</th>
              <th className="text-right px-5 py-2 font-medium">Amount</th>
              <th className="text-right px-5 py-2 font-medium">Retries</th>
              <th className="text-left px-5 py-2 font-medium">Reason</th>
              <th className="text-right px-5 py-2 font-medium">Updated</th>
            </tr>
          </thead>
          <tbody>
            {data.map((payout) => (
              <tr
                key={payout.id}
                className="border-t border-slate-100 hover:bg-slate-50"
              >
                <td className="px-5 py-2">
                  <span
                    className={`inline-flex items-center px-2 py-0.5 text-[11px] rounded-full ring-1 ${payoutStatusClasses[payout.status]}`}
                  >
                    {payout.status}
                  </span>
                </td>
                <td className="px-5 py-2 text-right tabular-nums">
                  {formatPaise(payout.amount_paise)}
                </td>
                <td className="px-5 py-2 text-right tabular-nums text-slate-500">
                  {payout.retry_count}
                </td>
                <td className="px-5 py-2 text-rose-700 truncate max-w-[24ch]">
                  {payout.failure_reason || "—"}
                </td>
                <td className="px-5 py-2 text-right text-slate-500">
                  {formatRelative(payout.updated_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

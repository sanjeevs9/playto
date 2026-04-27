import { useQuery } from "@tanstack/react-query";
import { fetchLedger } from "../api";
import { formatPaise, formatRelative } from "../lib/format";
import { ledgerTypeClasses } from "../lib/status";

export function LedgerFeed({ merchantId }: { merchantId: number }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["ledger", merchantId],
    queryFn: () => fetchLedger(merchantId),
    refetchInterval: 3000,
  });

  return (
    <section className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
      <header className="px-5 py-3 border-b border-slate-100">
        <h2 className="text-sm font-semibold text-slate-700">
          Ledger entries
        </h2>
        <p className="text-xs text-slate-500">
          Source of truth: balance ={" "}
          <code className="text-[11px]">SUM(amount_paise)</code> over this
          table.
        </p>
      </header>

      {isLoading ? (
        <div className="p-6 text-sm text-slate-500">Loading…</div>
      ) : error ? (
        <div className="p-6 text-sm text-rose-600">Failed to load ledger.</div>
      ) : !data || data.length === 0 ? (
        <div className="p-6 text-sm text-slate-500">No entries yet.</div>
      ) : (
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="text-left px-5 py-2 font-medium">Type</th>
              <th className="text-right px-5 py-2 font-medium">Amount</th>
              <th className="text-left px-5 py-2 font-medium">Description</th>
              <th className="text-right px-5 py-2 font-medium">When</th>
            </tr>
          </thead>
          <tbody>
            {data.map((entry) => (
              <tr
                key={entry.id}
                className="border-t border-slate-100 hover:bg-slate-50"
              >
                <td
                  className={`px-5 py-2 font-medium ${ledgerTypeClasses[entry.entry_type]}`}
                >
                  {entry.entry_type}
                </td>
                <td className="px-5 py-2 text-right tabular-nums">
                  {formatPaise(entry.amount_paise)}
                </td>
                <td className="px-5 py-2 text-slate-600 truncate max-w-[28ch]">
                  {entry.description || "—"}
                </td>
                <td className="px-5 py-2 text-right text-slate-500">
                  {formatRelative(entry.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

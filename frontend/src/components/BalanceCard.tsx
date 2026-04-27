import { useQuery } from "@tanstack/react-query";
import { fetchBalance } from "../api";
import { formatPaise } from "../lib/format";

export function BalanceCard({ merchantId }: { merchantId: number }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["balance", merchantId],
    queryFn: () => fetchBalance(merchantId),
    refetchInterval: 3000,
  });

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <Card label="Available balance">
        {isLoading
          ? "—"
          : error
            ? "Error"
            : data
              ? formatPaise(data.available_paise)
              : "—"}
      </Card>
      <Card label="Held in flight" muted>
        {isLoading
          ? "—"
          : error
            ? "Error"
            : data
              ? formatPaise(data.held_paise)
              : "—"}
      </Card>
    </div>
  );
}

function Card({
  label,
  children,
  muted = false,
}: {
  label: string;
  children: React.ReactNode;
  muted?: boolean;
}) {
  return (
    <div
      className={`rounded-xl bg-white border border-slate-200 px-5 py-4 shadow-sm ${
        muted ? "opacity-90" : ""
      }`}
    >
      <div className="text-xs uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div
        className={`mt-1 text-2xl font-semibold tabular-nums ${
          muted ? "text-slate-700" : "text-slate-900"
        }`}
      >
        {children}
      </div>
    </div>
  );
}

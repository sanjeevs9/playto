import { useQuery } from "@tanstack/react-query";
import { fetchMerchants } from "../api";

interface Props {
  value: number | null;
  onChange: (id: number) => void;
}

export function MerchantSelector({ value, onChange }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["merchants"],
    queryFn: fetchMerchants,
  });

  if (isLoading) return <div className="text-slate-500">Loading…</div>;
  if (error)
    return (
      <div className="text-rose-600 text-sm">
        Failed to load merchants. Is the backend running?
      </div>
    );
  if (!data || data.length === 0)
    return (
      <div className="text-amber-700 text-sm">
        No merchants seeded. Run <code>python manage.py seed</code>.
      </div>
    );

  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-slate-600">Merchant</span>
      <select
        className="border border-slate-300 rounded-md px-3 py-1.5 bg-white text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-300"
        value={value ?? ""}
        onChange={(e) => onChange(Number(e.target.value))}
      >
        <option value="" disabled>
          Pick a merchant…
        </option>
        {data.map((m) => (
          <option key={m.id} value={m.id}>
            #{m.id} — {m.name}
          </option>
        ))}
      </select>
    </label>
  );
}

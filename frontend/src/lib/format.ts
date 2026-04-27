/**
 * Money + date formatting helpers.
 *
 * Paise are stored as integers everywhere on the backend. The frontend
 * formats them as INR for display only — conversion to a decimal string
 * happens at the boundary, never in arithmetic.
 */

const inrFormatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

export const formatPaise = (paise: number): string => {
  // Division by 100 here is for DISPLAY only. Never round-trip back to paise
  // via a float — always work in integer paise on the backend.
  return inrFormatter.format(paise / 100);
};

export const formatDate = (iso: string | null): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  });
};

export const formatRelative = (iso: string | null): string => {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return formatDate(iso);
};

import type { LedgerEntryType, PayoutStatus } from "../api";

export const payoutStatusClasses: Record<PayoutStatus, string> = {
  PENDING: "bg-slate-100 text-slate-700 ring-slate-200",
  PROCESSING: "bg-blue-50 text-blue-700 ring-blue-200",
  COMPLETED: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  FAILED: "bg-rose-50 text-rose-700 ring-rose-200",
};

export const ledgerTypeClasses: Record<LedgerEntryType, string> = {
  CREDIT: "text-emerald-700",
  DEBIT: "text-rose-700",
  REFUND: "text-blue-700",
};

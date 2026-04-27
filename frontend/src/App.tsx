import { useEffect, useState } from "react";
import { BalanceCard } from "./components/BalanceCard";
import { LedgerFeed } from "./components/LedgerFeed";
import { MerchantSelector } from "./components/MerchantSelector";
import { PayoutForm } from "./components/PayoutForm";
import { PayoutHistory } from "./components/PayoutHistory";

export default function App() {
  const [merchantId, setMerchantId] = useState<number | null>(() => {
    const raw = localStorage.getItem("playto.merchantId");
    return raw ? Number(raw) : null;
  });

  useEffect(() => {
    if (merchantId != null) {
      localStorage.setItem("playto.merchantId", String(merchantId));
    }
  }, [merchantId]);

  return (
    <div className="min-h-screen">
      <Header merchantId={merchantId} setMerchantId={setMerchantId} />

      <main className="max-w-6xl mx-auto px-6 py-6 space-y-6">
        {merchantId == null ? (
          <EmptyState />
        ) : (
          <>
            <BalanceCard merchantId={merchantId} />
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <LedgerFeed merchantId={merchantId} />
              <div className="space-y-6">
                <PayoutForm merchantId={merchantId} />
                <PayoutHistory merchantId={merchantId} />
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}

function Header({
  merchantId,
  setMerchantId,
}: {
  merchantId: number | null;
  setMerchantId: (id: number) => void;
}) {
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">
            Playto Payout Engine
          </h1>
          <p className="text-xs text-slate-500">
            Merchant dashboard · paise ledger · idempotent payouts
          </p>
        </div>
        <MerchantSelector value={merchantId} onChange={setMerchantId} />
      </div>
    </header>
  );
}

function EmptyState() {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
      <h2 className="text-base font-semibold text-slate-900">
        Pick a merchant to get started
      </h2>
      <p className="mt-1 text-sm text-slate-500">
        The dashboard polls every 3 seconds while the worker drives payouts
        through their lifecycle.
      </p>
    </div>
  );
}

import axios from "axios";

export type PayoutStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";
export type LedgerEntryType = "CREDIT" | "DEBIT" | "REFUND";

export interface Merchant {
  id: number;
  name: string;
  email: string;
  created_at: string;
}

export interface Balance {
  merchant_id: number;
  available_paise: number;
  held_paise: number;
}

export interface LedgerEntry {
  id: number;
  amount_paise: number;
  entry_type: LedgerEntryType;
  description: string;
  related_payout_id: string | null;
  created_at: string;
}

export interface BankAccount {
  id: number;
  holder_name: string;
  account_number_last4: string;
  ifsc: string;
  nickname: string;
  is_default: boolean;
}

export interface Payout {
  id: string;
  merchant_id: number;
  bank_account_id: number;
  amount_paise: number;
  status: PayoutStatus;
  retry_count: number;
  failure_reason: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PayoutCreateError {
  error: string;
  available_paise?: number;
  requested_paise?: number;
  detail?: string;
}

const api = axios.create({ baseURL: "" });

export const fetchMerchants = () =>
  api
    .get<{ results: Merchant[] }>("/api/v1/merchants")
    .then((r) => r.data.results);

export const fetchBalance = (merchantId: number) =>
  api.get<Balance>(`/api/v1/merchants/${merchantId}/balance`).then((r) => r.data);

export const fetchLedger = (merchantId: number) =>
  api
    .get<{ results: LedgerEntry[] }>(`/api/v1/merchants/${merchantId}/ledger`)
    .then((r) => r.data.results);

export const fetchBankAccounts = (merchantId: number) =>
  api
    .get<{ results: BankAccount[] }>(
      `/api/v1/merchants/${merchantId}/bank-accounts`,
    )
    .then((r) => r.data.results);

export const fetchPayouts = (merchantId: number) =>
  api
    .get<{ results: Payout[] }>("/api/v1/payouts", {
      headers: { "X-Merchant-Id": String(merchantId) },
    })
    .then((r) => r.data.results);

export interface CreatePayoutInput {
  amount_paise: number;
  bank_account_id: number;
}

export interface CreatePayoutResult {
  ok: boolean;
  status: number;
  data: Payout | PayoutCreateError;
  idempotencyKey: string;
}

export const createPayout = async (
  merchantId: number,
  input: CreatePayoutInput,
): Promise<CreatePayoutResult> => {
  // Generated client-side so a network retry replays with the same key.
  // crypto.randomUUID is part of the standard browser/Node crypto API.
  const idempotencyKey = crypto.randomUUID();
  try {
    const r = await api.post<Payout>("/api/v1/payouts", input, {
      headers: {
        "X-Merchant-Id": String(merchantId),
        "Idempotency-Key": idempotencyKey,
      },
    });
    return { ok: true, status: r.status, data: r.data, idempotencyKey };
  } catch (err) {
    if (axios.isAxiosError(err) && err.response) {
      return {
        ok: false,
        status: err.response.status,
        data: err.response.data as PayoutCreateError,
        idempotencyKey,
      };
    }
    throw err;
  }
};

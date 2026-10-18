import { fmtINR } from "@/lib/api";

/** Rupee amounts written as raw digits ("2,000,000,000", "1880000000") -> "₹200 Cr". Seven or more digits only,
 *  so dates, page numbers and ids are left alone. */
export const moneyText = (s?: string | null) =>
  (s ?? "").replace(/(?<![\w-])(\d{1,3}(?:,\d{2,3}){2,}|\d{7,})(?:\.\d+)?(?![\w-])/g, (m) => {
    const n = Number(m.replace(/,/g, ""));
    return n >= 1e6 && n < 1e13 ? fmtINR(n) : m;
  });

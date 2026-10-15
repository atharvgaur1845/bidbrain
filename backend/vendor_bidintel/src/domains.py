"""Route a question to a fact block, then filter and aggregate it generically.

`answer.py` resolves every question to the `works` table. That is right for the
13 shapes the samples cover and wrong for the rest: a plant register that is
extracted but unreachable scores exactly as badly as one that was never read
(`decision_log.md` s7). This module is the reachability layer.

It is deliberately *declarative*. Each block registers the vocabulary that
identifies it, the numeric fields it can aggregate and the dimensions it can be
filtered on; one generic FILTER -> AGGREGATE path then serves every block. Adding
a fact source is a table entry, not a new code path, and no entry names a
question, an answer, or a specific person / client / work.

Routing reads *domain vocabulary only* — never dimension values, with one
measured exception. Client names appear in `works` and in `invoices` alike, so
letting a client name vote would hand "this client's completed works" to the
receivables ledger; shared values still cannot vote. But a value owned by
*exactly one* block ("Tunnel Boring Machine" appears nowhere but `assets`) is
as safe a signal as the block's own vocabulary, and is scored the same way —
see `_value_evidence`.
"""
from __future__ import annotations

import collections
import dataclasses
import re
import statistics

CRORE, LAKH = 10_000_000, 100_000


@dataclasses.dataclass(frozen=True)
class Domain:
    """One fact block, and how a question reaches it."""

    block: str
    evidence: str                      # vocabulary that identifies this domain
    measures: dict[str, str]           # numeric field -> synonyms that select it
    # Terms that cannot plausibly appear in a question about completed works —
    # "bond", "receivable", "turnover". One is enough to route on its own,
    # because a works question that says "bond" does not exist. Kept separate
    # from `evidence` so the weak, shared vocabulary can never route alone.
    strong: str = ""
    default_measure: str | None = None
    dims: tuple[str, ...] = ()         # categorical fields, values read from data
    flags: tuple[str, ...] = ()        # boolean fields
    fy_field: str | None = None
    date_fields: tuple[str, ...] = ()
    # A document with a validity window is live or lapsed by its expiry date,
    # which is what "still live" / "expired" ask about — not by a status word.
    expiry_field: str | None = None
    # Where a question's word for a value differs from the word the document
    # stores: field -> {stored value: regex the question would use}. Ordered
    # most specific first, since "unpaid" contains "paid".
    dim_synonyms: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)
    # dim -> the phrasing that means "the complement of the class just matched".
    # "How many requirements did we fail to meet" names the met class and then
    # negates it; without this the negation is silently dropped and the question
    # is answered backwards, which is worse than not answering it.
    negate: dict[str, str] = dataclasses.field(default_factory=dict)
    # dim -> the noun a question uses for it, where that differs from the column
    # name. "How many separate tenders" has to reach `tender_ref`.
    dim_nouns: dict[str, str] = dataclasses.field(default_factory=dict)
    # Integer identifier columns, named as "<field> <n>": "contract 74",
    # "RA bill 3". Not dims: their values are numbers, and the value matcher
    # deliberately ignores short numeric tokens.
    key_fields: tuple[str, ...] = ()
    # Measures no document prints as a line, but every tender asks for:
    # name -> (synonyms, {source field: coefficient}). Computed per record, so
    # they filter and aggregate exactly like a stored column.
    derived: dict[str, tuple[str, dict[str, int]]] = dataclasses.field(default_factory=dict)
    year_field: str | None = None      # an integer year column
    # Word-anchored: an unanchored "count of" fires inside "headcount of" and
    # turns "what is the headcount of X" into a record count of 1.
    counts: str = r"\bhow many\b|\bnumber of\b|\bcount of\b"
    # Tie-break only. Two blocks holding the same data by construction (the
    # trial-balance workbook reproduces the ledger totals exactly, 194/194 in
    # the audit) would otherwise deadlock the ambiguity guard forever.
    priority: int = 0
    # "How many people do we employ" is a sum of a headcount column, not a
    # count of rows. `record_noun` is what one row *is*, so a question that
    # names it ("how many business units") still counts rows.
    count_measure: str | None = None
    record_noun: str = ""

    def score(self, q: str) -> int:
        """Distinct vocabulary hits. Measure synonyms count too, but at half
        weight — "value" and "cost" are common to every domain.

        A block's own *field names* are domain vocabulary as well ("asset
        types", "headcount"), so they score like a measure. Only names unique
        to one block and absent from the works table count — see
        DISTINCTIVE_FIELDS, which is derived, not hand-listed.
        """
        hits = len({m.group(0).lower() for m in re.finditer(self.evidence, q, re.I)})
        score = 2 * hits + sum(
            1 for pat in self.measures.values() if re.search(pat, q, re.I))
        if self.strong and re.search(self.strong, q, re.I):
            score += ROUTE_FLOOR
        return score + sum(
            1 for f in self.fields() if f in DISTINCTIVE_FIELDS and _names_field(q, f))

    def fields(self) -> tuple[str, ...]:
        return tuple(self.measures) + self.dims + self.flags + self.key_fields


# Vocabulary is drawn from what a bid desk calls these things, not from any
# sample question. Terms shared with the works table (value, client, project)
# are excluded on purpose so they cannot pull a works question off course.
DOMAINS: tuple[Domain, ...] = (
    Domain(
        block="assets",
        strong=(r"plant\b|machinery|asset register|plant register|fleet|excavator|"
                r"batching|paver|bulldozer|construction equipment|equipment register|"
                r"equipment fleet"),
        evidence=(r"plant\b|machinery|machines?\b|equipment|asset register|asset\b|fleet|"
                  r"excavator|crane|batching|paver|roller|grader|tipper|drill|"
                  r"owned outright|on lease|leased|safety[- ]certified|"
                  r"deployed|at site|mobilis\w+|mobiliz\w+"),
        measures={"cost": r"cost|acquisition|worth|book value|capital|value"},
        default_measure="cost",
        dims=("asset_type", "condition", "ownership", "location", "make"),
        # `location` values in this corpus are Indian states; "state" is the
        # noun a bid desk actually uses ("each state we operate in"), not the
        # column name.
        dim_nouns={"location": r"states?|regions?|locations?"},
        flags=("safety_certified",),
        year_field="acquired",
    ),
    Domain(
        block="invoices",
        # A bare "raised on" is not receivables vocabulary: a running account
        # bill is raised on a contract. It stays in `evidence`, where it needs
        # corroboration, and out of `strong`, which routes on its own.
        # "Realisation/collection/recovery rate" must win outright: `boq_items`
        # also carries a bare `\brate\b` (its own unit-rate column), so on
        # score alone "realisation rate against <client>" scored boq_items 4
        # and invoices 3 — the block-name-collision failure of `decision_log.md`
        # s9(e), but between two blocks' vocabulary rather than within one.
        # "Still owes us" / "the client we have billed" are group-by phrasings
        # (PC-06, PC-07, `experiment_log.md` §11) that name no receivables word
        # from the lists below at all, or name "billed" — which `contracts`
        # also claims for `billed_value`. Both are strong on their own: a
        # completed-works question never discusses what a client owes, and
        # "client ... billed" (in either order) is a receivables framing,
        # while a contracts question about billing names the contract, not
        # the client.
        strong=(r"invoices?\b|receivables?\b|ageing|aging|overdue|debtors?\b|"
                r"bills? raised on (?!contract)|shown as due|unsettled|"
                r"realisation rate|realization rate|collection rate|recovery rate|"
                # A *balance* in a receivables sentence is money a client owes, not
                # a trial-balance row. Without these, `invoices` and
                # `trial_balance` tied at 3 and the tie went to `trial_balance`
                # on the `priority` §8 gave it for an unrelated reason — and a
                # whole-ledger balance sum is identically 0 (see `_DEGENERATE`
                # below), so four evaluation questions scored a certain zero
                # (`experiment_log.md` §24).
                r"balance (?:still )?(?:owed|due|outstanding|remaining)|"
                r"(?:remaining|unpaid|outstanding|net|adjusted|true|system) balance|"
                r"still (?:sitting )?on (?:our|the) books|on our books|"
                r"still owed|still due|amount owed|owed (?:to us|across)|"
                r"owes? us|owed by (?:them|the client)|still owes|"
                # A cluster of six near-identical receivables questions sat in
                # the evaluation set and only two of them routed here -- one
                # because it said "invoices", one because it said "unpaid". The
                # other four said "still pending", "total pending amount",
                # "currently due", "submitted charges"/"submitted bills", none
                # of which was receivables vocabulary anywhere, and all four
                # were answered as the client's whole works portfolio
                # (`experiment_log.md` §38). `pending` is matched as a noun
                # phrase rather than bare: a bid can be pending, a tender can be
                # pending, but a *bill* that is pending is a receivable.
                r"still pending|pending amount|"
                r"(?:submitted|outstanding|unsettled) (?:charges|bills)|"
                r"client[^.?!]{0,20}\bbilled\b|\bbilled\b[^.?!]{0,20}client"),
        evidence=(r"invoice|receivable|ageing|aging|outstanding|overdue|unpaid|"
                  r"debtor|realisation|realization|collected|not yet (?:been )?paid|"
                  r"still owed|owed to us|billed and|"
                 # "the total amount **they still owe** for all the work we've
                 # billed them for" scored **1** and fell below `ROUTE_FLOOR`,
                 # so a receivables question was answered as the client's
                 # portfolio value. The vocabulary knew "still owed" (passive)
                 # and "owes us", but not the commonest active form
                 # (`experiment_log.md` §31).
                 r"they still owe|still owe (?:us|for)|owe us|amount they owe|raised against|raised on|"
                  r"bills? raised|unsettled|uncollected|shown as due|"
                  # Money "received from" someone is receivables framing. The
                  # bare verb stays out — a work is *received* by a client too —
                  # but nothing else in this estate is received *from* one.
                  r"receive[ds]? from|received against"),
        # Noun phrases only. A bare "still due" is a *predicate* on invoices
        # ("what share of receivables is still due" — a status filter), while
        # "the amount still due" names the quantity. Matching the bare form as
        # a measure turned that share into 114% by measuring `outstanding`
        # against `outstanding` (`experiment_log.md` §24).
        measures={"outstanding": r"outstanding|overdue|unpaid|still owed|owes? us|"
                                  r"(?:remaining|unpaid|outstanding|net|adjusted|true|"
                                  r"system) balance(?:\s+due)?|"
                                  r"balance (?:still )?(?:owed|due|outstanding|remaining)|"
                                  # "still" was the only adverb the noun phrase
                                  # allowed, so "the total amount **currently
                                  # due**" fell to the block's default measure
                                  # and reported what we had *billed*. Same
                                  # widening for `pending`, which names the same
                                  # state as `due` on a bill
                                  # (`experiment_log.md` §38).
                                  r"(?:amount|total|sum|figure)\s+"
                                  r"(?:(?:still|currently|now|presently|as yet)\s+)?"
                                  r"(?:due|owed|outstanding|unpaid|remaining|pending)|"
                                  # "our billed totals **is still pending**",
                                  # "billed amounts that **are still pending**"
                                  # -- the state follows a verb, so the noun
                                  # phrase above cannot reach it.
                                  r"\bstill pending\b|"
                                  r"owed by (?:them|the client)|not (?:yet )?(?:been )?(?:paid|received)|uncollected|"
                                  # "the total **amount they still owe**" — the
                                  # active voice with the subject in the middle,
                                  # which the passive forms above do not reach.
                                  # Routing found the right block and the
                                  # measure defaulted to what we had *billed*.
                                  r"(?:amount|total|sum|figure)\s+(?:that\s+)?they\s+(?:still\s+)?owe\b|"
                                  # Three questions asked what is left on the
                                  # ledger without using any of the nouns above
                                  # — "what **amount remains** on the invoices",
                                  # "what is **the balance** when I cross-check
                                  # invoices against the payment system", "the
                                  # balance still on their end". Each fell to
                                  # the block's default measure and reported
                                  # what we *billed* (`experiment_log.md` §29).
                                  # Still noun phrases, for the reason above.
                                  # A bare "the balance" is NOT enough: "the
                                  # closing balance on Accounts Receivable in
                                  # FY 2023-24" is a trial-balance question,
                                  # and matching it here pulled `PC-27` onto
                                  # the invoices block — caught by the ratchet
                                  # on the same run the widening landed.
                                  r"(?:amount|value|figure|sum)\s+(?:that\s+)?remains?\b|"
                                  r"\bwhat remains\b|balance (?:when|after|we|i)\b|"
                                  r"balance still on (?:our|their|the)\b",
                  # `receiv\w+` would swallow "receivables", which names the
                  # ledger, not the money that came in.
                  "received": r"\breceive[ds]?\b|\breceiving\b|collect\w*|realis\w*|realiz\w*|receipts?|recover\w*|paid to us",
                  "invoiced": r"invoiced|billed|raised|invoice value"},
        default_measure="invoiced",
        dims=("client", "status"),
        date_fields=("invoice_date",),
        # The ledger says `due`; a bid desk says overdue, unpaid, outstanding.
        dim_synonyms={"status": {
            "part_paid": r"part[- _]?paid|partly paid|partially (?:paid|settled)",
            "due": r"\bdue\b|overdue|unpaid|not (?:yet )?(?:been )?paid|outstanding|pending|uncollected",
            "paid": r"\bpaid\b|settled|cleared|realised|realized|collected in full",
        }},
    ),
    Domain(
        block="trial_balance",
        strong=r"trial balance",
        # `\b`-anchored: a bare "credit" matches inside "accreditations" and
        # "debit" is common enough as a fragment to deserve the same guard.
        evidence=r"trial balance|account balance|closing balance|\bdebit\b|\bcredit\b|ledger balance",
        # "The TRIAL BALANCE for FY..." is the block's own name, not a request
        # for the balance column — and being a bare match it silently
        # outscored "debit" on raw length whenever a question said both,
        # exactly the "bill of quantities" collision in decision_log.md s9(e).
        measures={"balance": r"(?<!trial )balance", "debit": r"\bdebit\b", "credit": r"\bcredit\b"},
        default_measure="balance",
        dims=("account",),
        fy_field="fy",
        # Preferred over `ledger_accounts` when both match: the workbook is the
        # structured rendering of the same totals, and the ledger PDFs are a
        # cross-check by decision (decision_log.md s7).
        priority=1,
    ),
    Domain(
        block="boq_items",
        strong=r"\bboq\b|bill of quantit",
        evidence=(r"\bboq\b|bill of quantit|line item|unit rate|\brate\b|"
                  r"quantit\w+|\bcum\b|\bsqm\b|\bmt\b|per unit"),
        # "bill of quantities" is the block's *name*, not a request for the
        # quantity column — and being the longer match it would beat "value".
        measures={"amount": r"amount|value|worth|total", "rate": r"rate|per unit|unit price",
                  "quantity": r"(?<!bill of )quantit\w+|volume"},
        default_measure="amount",
        dims=("description", "unit"),
        # The unit's spelled-out name ("cubic metres") is how a bid desk names
        # a quantity, not the stored abbreviation ("cum") the ledger prints.
        dim_synonyms={"unit": {
            "cum": r"cubic met(?:re|er)s?|\bcum\b",
            "MT": r"metric tonnes?",
            "rmt": r"running met(?:re|er)s?",
            "LS": r"lump sum",
        }},
        # "Rate PER UNIT" and "UNIT price" are this block's own `rate` measure
        # synonyms (above), not a reference to the `unit` dimension — the
        # group-by mechanism otherwise reads "average rate per unit" as
        # "group by unit", the same collision class as "bank guarantee" for
        # `bonds.bank` (decision_log.md s13).
        dim_nouns={"unit": r"(?<!per )\bunits?\b(?!\s*price)"},
        key_fields=("contract",),
        # "How many cubic metres" is a sum of the quantity column, not a count
        # of BOQ rows; "how many line items" is the opposite. Same shape as
        # `business_units.count_measure`, gated the other way round: sum unless
        # the question names the rows themselves.
        count_measure="quantity",
        record_noun=r"line items?|boq items?",
    ),
    Domain(
        block="measurements",
        strong=r"measurement register|measured quantit",
        evidence=(r"measurement|measured|running account|\bra bill|\bra-\d|"
                  r"measurement register|remeasure"),
        # "measured" is this block's own name; leaving it in the `qty` synonyms
        # made "the total value measured on contract 72" a sum of cubic metres.
        measures={"amount": r"amount|value|worth", "qty": r"quantit\w+|\bqty\b|volume"},
        default_measure="amount",
        dims=("description",),
        date_fields=("measured_on",),
        key_fields=("contract", "ra_no"),
    ),
    Domain(
        block="staff",
        # Attribute questions about individual engineers. Company-wide people
        # counts belong to `business_units`: the corpus holds 39 CVs but 486
        # employees, so counting CVs would understate the workforce 12-fold.
        strong=r"years of experience|wage group|qualification|date of joining",
        evidence=(r"\bstaff\b|headcount|head count|employees?\b|payroll|"
                  r"designation|qualification|years of experience|experience of|"
                  r"on the rolls|workforce|manpower|key personnel|technical staff|"
                  r"wage group|joined|graduate|diploma|\bb\.?tech\b|\bm\.?tech\b"),
        measures={"experience_years": r"experience|years"},
        default_measure=None,
        dims=("designation", "business_unit", "qualification", "wage_group", "person"),
        # Wage grades are single-letter codes ("A".."D"), and the generic
        # value matcher requires a 4-character literal or a 3-character token
        # by design (decision_log.md s9 — the guard that stops "TDS Payable"
        # colliding with "Accounts Payable") — a bare letter can never clear
        # that bar. Anchored to the phrase a bid desk actually uses, not a
        # bare letter, so it cannot fire on an unrelated single "C" elsewhere.
        dim_synonyms={"wage_group": {
            "A": r"wage (?:group|band)\s*a\b|\bgrade\s*a\b",
            "B": r"wage (?:group|band)\s*b\b|\bgrade\s*b\b",
            "C": r"wage (?:group|band)\s*c\b|\bgrade\s*c\b",
            "D": r"wage (?:group|band)\s*d\b|\bgrade\s*d\b",
        }},
        date_fields=("joined",),
    ),
    Domain(
        block="business_units",
        # The company-wide people questions land here, not on the CV block.
        # Summing headcount over the 6 units reproduces the README's 486
        # employees on record exactly.
        strong=(r"business units?\b|business segments?\b|headcount|head count|"
                r"payroll|workforce|manpower|on the rolls|employees? on record|"
                r"total employees|how many (?:people|employees|staff) (?:do we|does the company)"),
        evidence=(r"business unit|division|vertical|business segment|headcount|"
                  r"employees?\b|staff\b|people\b|workforce|manpower"),
        measures={"headcount": r"headcount|head count|staff|employees?|people|strength"},
        default_measure="headcount",
        dims=("unit", "scale"),
        count_measure="headcount",
        record_noun=r"business units?|segments?|divisions?|verticals?",
    ),
    Domain(
        block="financials",
        strong=r"turn\s?over|net worth|networth|balance sheet|profit after tax|profit before tax|revenue from operations|order book|working capital|\bpat\b|\bpbt\b|financial statements?\b",
        evidence=(r"turn\s?over|revenue|net worth|networth|profit|\bpat\b|\bpbt\b|"
                  r"balance sheet|financial statement|statutory account|"
                  r"working capital|current assets|current liabilit|borrowing|"
                  r"reserves|paid[- ]up|share capital|depreciation|order book|"
                  r"fiscal year|financial year|\bfy\b|annual accounts|"
                  r"employee benefit|cost of materials|subcontract|"
                  r"trade payable|trade receivable|inventor|intangible|"
                  r"tangible|\bppe\b|earnings|top line|bottom line"),
        measures={
            "revenue_operations": r"turn\s?over|revenue from operations|top line|gross revenue|annual revenue|revenue",
            "contract_revenue": r"contract revenue|construction revenue",
            "pat": r"\bpat\b|profit after tax|net profit|bottom line|earnings",
            "pbt": r"\bpbt\b|profit before tax",
            "tax": r"\btax\b",
            "depreciation": r"depreciation",
            "employee_benefit_expense": r"employee benefit|staff cost|payroll cost",
            "cost_of_materials": r"cost of materials|material cost",
            "subcontracting_labour": r"subcontract|labour",
            "total_expenses": r"total expenses|total cost",
            "other_expenses": r"other expenses",
            "paid_up_capital": r"paid[- ]up|share capital",
            "reserves": r"reserves|surplus",
            "long_term_borrowings": r"borrowing|long[- ]term debt|term loan",
            "trade_payables": r"trade payable|payable",
            "trade_receivables": r"trade receivable|sundry debtor",
            "inventories": r"inventor|stock in hand",
            "cash": r"\bcash\b|bank balance",
            "ppe": r"\bppe\b|property, plant|fixed asset|tangible asset",
            "intangible_assets": r"intangible",
            "order_book": r"order book|order backlog|unexecuted",
        },
        default_measure="revenue_operations",
        fy_field="fy",
        # Net worth and working capital are the two figures a tender's
        # financial-capacity gate is written in, and neither is a printed line.
        derived={
            "net_worth": (r"net worth|networth|shareholders?.{0,3} funds|owners?.{0,3} equity",
                          {"paid_up_capital": 1, "reserves": 1}),
            "working_capital": (r"working capital|current ratio|liquidity",
                                {"inventories": 1, "trade_receivables": 1, "cash": 1,
                                 "trade_payables": -1, "other_current_liabilities": -1}),
            "gross_block": (r"gross block|fixed assets|capital employed",
                            {"ppe": 1, "intangible_assets": 1}),
        },
    ),
    Domain(
        block="annual_reports",
        strong=r"annual reports?\b|gross billings?",
        evidence=r"annual report|gross billing|net revenue",
        measures={"gross_billings": r"gross billing|billing", "net_revenue": r"net revenue|revenue",
                  "profit": r"profit|earnings"},
        default_measure="gross_billings",
        fy_field="fy",
    ),
    Domain(
        block="contracts",
        strong=r"awarded value|billed value|final bills?\b|running account bills?",
        # `PD-11` established that the *bare* word "contract" cannot be routing
        # vocabulary — it pushed a sample question to `ROUTE_FLOOR` and was
        # reverted (`decision_log.md` §14(e)). These are the narrower phrases
        # batch 4 wanted: the *act* of awarding or billing spoken of as ours
        # ("awarded to us", "billed on contract"), which a completed-works
        # question does not say — measured at 1 against every sample, where the
        # bare word reached the floor.
        #
        # `our contracts` was tried here too and reverted within the same pass:
        # "across any of our contracts" is how `PD-08` scopes a *measurements*
        # question, so the phrase names the denominator of a question, not its
        # subject. It tied `contracts` with `measurements` at 3 and §8's
        # tie rule sent the question to `works`. Scope words are not subject
        # words — the distinction the bare-noun revert was really about.
        evidence=(r"awarded value|contract value awarded|billed value|"
                  r"final bill|running account bill|\bra bills?\b|contract period|"
                  r"variation|revised contract|"
                  r"awarded to us|value awarded|billed on contract"),
        measures={"awarded_value": r"awarded|award value|contract value",
                  "billed_value": r"billed|billing|certified",
                  "n_ra_bills": (r"how many (?:ra |r\.a\. |running account )?bills|"
                                 r"number of (?:ra |running account )?bills")},
        default_measure="awarded_value",
        dims=("client",),
        date_fields=("period_from", "period_to"),
        key_fields=("contract",),
        # "How many RA bills were raised on contract 73" totals a bill-count
        # column; "how many contracts do we hold" counts rows. The lookahead
        # keeps "contract 73" — which names a key, not the record noun — out.
        count_measure="n_ra_bills",
        record_noun=r"\bcontracts?\b(?!\s*(?:no\.?|number|#)?\s*\d)",
    ),
    Domain(
        block="bonds",
        strong=(r"performance bonds?\b|bank guarantees?\b|\bguarantees?\b|\bbonds?\b|surety|"
                r"security deposit|performance securit\w+|performance guarantees?"),
        evidence=(r"performance bonds?\b|bank guarantee|\bbg\b|guarantee|bonds?\b|"
                  r"security deposit|surety|exposure|issued by the bank|"
                  r"\bencash\w*|\bexpir\w+|\brelease\w*|\blive\b|\boutstanding bond"),
        measures={"amount": r"amount|value|worth|exposure|cover"},
        default_measure="amount",
        dims=("bank", "status", "work_description", "tender_ref"),
        # "Bank guarantee" is this block's own name (`evidence`, `strong`), not
        # a reference to the issuing-bank dimension — "the smallest bank
        # guarantee" ranks guarantees, not banks. The negative lookahead is the
        # same class of guard as the block-name exclusions in `decision_log.md`
        # s9(e), narrowed to the one phrase that collides.
        dim_nouns={"bank": r"\bbanks?\b(?!\s+guarantees?)"},
        date_fields=("issued", "expiry"),
        expiry_field="expiry",
    ),
    Domain(
        block="certifications",
        # "accreditation" cannot plausibly appear in a completed-works question,
        # so it is strong on its own — the same reasoning as "bond" or
        # "turnover". Without it, "accreditations" alone scored only 2 (one
        # evidence hit) and lost the floor to a domain whose "credit" measure
        # matched a bare substring of the word (fixed above).
        strong=r"\biso\b|9001|14001|45001|accreditation",
        # "Certifying bodies" (plural) scored only 2 — the singular-only
        # "certifying body" missed it — and "due to expire" named nothing at
        # all: `bonds` already has `\bexpir\w+` for the same concept.
        evidence=(r"\biso\b|certification|accreditation|9001|14001|45001|"
                  r"quality management|environmental management|"
                  r"occupational health|certifying bod(?:y|ies)|\bexpir\w+"),
        measures={},
        dims=("standard", "body", "cert_no"),
        # The naive `dim + "s?"` pluralisation ("bodys?") misses the actual
        # English plural — the same irregular-plural gap as `party`/`parties`
        # would be, just never exercised before "certifying bodies" asked it.
        dim_nouns={"body": r"bod(?:y|ies)"},
        date_fields=("initial_date", "valid_until"),
        # "Still valid" / "expired" asks about the certificate's own validity
        # window, the same mechanism `bonds` already uses for "still live".
        expiry_field="valid_until",
    ),
    Domain(
        block="compliance",
        # "Minimum average turnover" is the compliance matrices' own standard
        # phrasing for a tender gate (`financials.py`'s cross-check even
        # quotes it verbatim) — but "turnover" alone is also a `financials`
        # strong marker, and the two tied at equal score on a question naming
        # only this phrase, which a tie falls back to `works` from (s8), not
        # to either domain. The full gate phrase is unambiguous either way.
        strong=(r"compliance matri|complied|eligibility criteri|qualifying criteri|"
                r"minimum average turnover|turnover requirement|turnover gate"),
        evidence=(r"compliance matrix|complian\w+|requirement|checklist|"
                  r"tender gate|eligibility criteri|qualifying criteri|"
                  r"not complied|met the requirement"),
        measures={"threshold_value": r"threshold|minimum|gate|criteri"},
        dims=("status", "requirement", "tender_ref", "matrix_id"),
        # Two matrices vocabularies say the same thing: `MET` in one layout,
        # `Complied` in the other. Both are the same class to a question.
        dim_synonyms={"status": {
            "MET": r"\bmet\b|\bmeet\b|complied|compliant|satisf\w+|fulfil\w*",
            "Complied": r"\bmet\b|\bmeet\b|complied|compliant|satisf\w+|fulfil\w*",
        }},
        negate={"status": r"not complied|non[- ]complian\w+|fail\w* to (?:meet|comply)|"
                          r"did not meet|\bunmet\b|shortfall|deficien\w+|not met"},
        dim_nouns={"tender_ref": r"tenders?\b|rfps?\b|bids?\b",
                   "requirement": r"requirements?|criteri\w+|conditions?",
                   # "How many separate compliance matrices" must reach
                   # `matrix_id`, not fall through to the first dim in the
                   # tuple (`status`, 2 distinct values) for want of a noun.
                   "matrix_id": r"matri(?:x|ces)\b|checklists?"},
    ),
    Domain(
        block="ledger_accounts",
        strong=r"general ledger|ledger book|journal\b|account code",
        evidence=(r"general ledger|journal|ledger book|debit total|credit total|"
                  r"account code"),
        measures={"closing_balance": r"closing|balance", "debit_total": r"\bdebit\b",
                  "credit_total": r"\bcredit\b"},
        default_measure="closing_balance",
        dims=("account_name", "account_code"),
        fy_field="fy",
    ),
)

BY_BLOCK = {d.block: d for d in DOMAINS}

# Columns of the works table. A field name shared with works is not evidence of
# anything: "client", "category" and "value" are how the 13 known shapes are
# phrased, and letting them vote would hand works questions to the receivables
# ledger.
WORKS_FIELDS = frozenset({
    "work", "client", "category", "value", "completed", "grading", "role",
    "project_lead", "cert_ref", "has_reference", "person",
})


def _field_owners() -> dict[str, int]:
    owners: dict[str, int] = {}
    for d in DOMAINS:
        for f in d.fields():
            owners[f] = owners.get(f, 0) + 1
    return owners


# Unique to one block, and not a works column. Derived so that adding a domain
# re-derives it — a name that stops being distinctive stops voting on its own.
DISTINCTIVE_FIELDS = frozenset(
    f for f, n in _field_owners().items() if n == 1 and f not in WORKS_FIELDS)


def _names_field(question: str, field: str) -> bool:
    """Does the question use this column's name? "asset_type" -> "asset type",
    "asset types"."""
    words = field.replace("_", r"\s*")
    return re.search(rf"\b{words}s?\b", question, re.I) is not None

# A question must clear this before it leaves the works table.
#
# Set by measurement, not taste: the highest score any of the 25 works-shaped
# sample questions reaches on any domain is 2 (four of them touch
# `certifications` through the word "certification"; the rest score 0 or 1). A
# floor of 3 therefore clears every known works question with a margin of one,
# while admitting the real domain questions, which score 3 when they name their
# subject once and their measure once ("the total BOQ amount for contract 71").
# Asserted by test_router_leaves_works_questions_alone; re-measure this if the
# vocabulary changes.
ROUTE_FLOOR = 3


def route(question: str, facts: dict) -> Domain | None:
    """The block this question is about, or None to mean the works table.

    None is the safe default in both directions: the works path is the tested
    one, and every shape the samples cover lives there.
    """
    # A cross-block question names `works` as its subject and another
    # domain's vocabulary only in a clause describing *which* works are in
    # scope ("... for clients who still owe us money") — a domain score
    # cannot tell that apart from a question actually about the domain, since
    # "owe us" is real evidence either way. "works" is unambiguous: no domain
    # claims that word (`WORKS_FIELDS`), so naming it as the subject overrides
    # any domain score. The cross-block filter itself lives in `answer.py`,
    # which is what this fall-through to `None` reaches.
    if re.search(r"\bcompleted works\b|\bworks we (?:hold|have|deliver\w*)\b|\bour works\b", question, re.I):
        return None
    universe = _all_text_values(facts)
    owners = _value_owners(universe)
    # A question that names its subject project quotes that project's title, and
    # a title is not evidence about a block. "Material Handling Plant — Uttar
    # Pradesh Pkg-47" and "Water Treatment Plant — Rajasthan Pkg-58" both
    # contain `plant\b`, which is the assets block's STRONGEST marker, and that
    # one word routed eleven works questions onto the plant register — asking
    # for the combined value of a client's completed assignments and being
    # answered with the cost of machinery in that state (`experiment_log.md`
    # §22). The value-ownership guard could not catch it: it checks dimension
    # *values*, and "plant" is vocabulary, not a value.
    scored_text = _mask_work_titles(question, facts)
    ranked = sorted(
        ((d.score(scored_text) + _value_evidence(question, d, facts, owners, universe),
          d.priority, d) for d in DOMAINS if facts.get(d.block)),
        key=lambda p: (p[0], p[1], p[2].block), reverse=True)
    if not ranked or ranked[0][0] < ROUTE_FLOOR:
        return None
    # An ambiguous winner is not a winner: fall back to works rather than guess.
    # A declared priority settles blocks that are redundant by construction; a
    # tie on both score and priority is real ambiguity and is not resolved here.
    if len(ranked) > 1 and ranked[1][:2] == ranked[0][:2]:
        return None
    return ranked[0][2]


_TITLE_CACHE: dict[int, list[str]] = {}


def _work_title_phrases(facts: dict) -> list[str]:
    """The distinct project-type phrases works are titled with.

    A work is `"<type> — <state> Pkg-<n>"`, so the part before the dash
    ("Material Handling Plant", "Water Treatment Plant", "Rigid Pavement") is
    the *kind* of project. There are a few dozen of them across 155 works, and
    a question naming one is identifying its subject, not using vocabulary.

    Only phrases of two words or more are returned: a single-word type would
    blank too much of the question, and the multi-word ones are what collide
    with block vocabulary in practice. Longest first, so the fullest title is
    masked rather than a fragment of it.
    """
    key = id(facts)
    if key not in _TITLE_CACHE:
        phrases = set()
        for w in facts.get("works", []):
            head = re.split(r"[—-]", str(w.get("work") or ""), maxsplit=1)[0].strip()
            if head and len(head.split()) >= 2:
                phrases.add(head)
        _TITLE_CACHE[key] = sorted(phrases, key=len, reverse=True)
    return _TITLE_CACHE[key]


def _mask_work_titles(question: str, facts: dict) -> str:
    """Blank the stretches of a question that quote a project's own title.

    Used for *vocabulary* scoring only. Value evidence still reads the original
    question, because a dimension value the question genuinely names must keep
    counting — this removes the subject's name, not the question's content.
    """
    masked = question
    for phrase in _work_title_phrases(facts):
        masked = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", masked, flags=re.I)
    return masked


def _overlaps(a: tuple[int, int], b: tuple[int, int] | None) -> bool:
    return b is not None and a[0] < b[1] and b[0] < a[1]


def _blank(text: str, span: tuple[int, int] | None) -> str:
    """`text` with `span` replaced by spaces of the same width.

    Blanked rather than cut, for the reason `_match_values` gives: measure and
    operation spans are read by character position, so removing characters would
    silently move every offset after them.
    """
    if span is None:
        return text
    start, end = span
    return text[:start] + " " * (end - start) + text[end:]


def _distinct(records: list[dict], field: str) -> list[str]:
    """Values actually present, longest first so the most specific matches."""
    seen = {str(r[field]) for r in records if r.get(field) not in (None, "")}
    return sorted(seen, key=len, reverse=True)


def _mentions(question: str, value: str) -> bool:
    """Does the question name this value?

    Exact substring, else a strong token overlap so shortened forms ("Jal Nigam
    Jharkhand") still land. Single short words are matched on their stem, since
    a register says `owned` / `leased` where a question says "own outright" or
    "on lease" — matching the stored form literally would miss every one.
    """
    low, val = question.lower(), value.lower()
    # Bounded, not a bare substring. `condition = "fair"` is four characters and
    # matched inside "I'm **fair**ly certain" — a phrase six evaluation questions
    # open with — which diverted every one of them onto the plant register and
    # answered a works question with a machinery cost (`experiment_log.md` §22).
    # The single-token path below has always been bounded; this shortcut skipped
    # it. Lookarounds rather than \b, so values ending in punctuation
    # ("Meridian Constructors & Co.") still match.
    if len(val) > 3 and re.search(rf"(?<!\w){re.escape(val)}(?!\w)", low):
        return True
    # Three-letter tokens carry meaning — `TDS Payable` and `Accounts Payable`
    # differ only in one, and dropping it made a question about one of them
    # quietly sum both.
    tokens = [t for t in re.split(r"\W+", val) if len(t) >= 3]
    if not tokens:
        return False
    if len(tokens) == 1:
        # One inflection off, not any word sharing a prefix: `\bcompli\w*` also
        # matches "compliance", which turned "how many compliance matrices" into
        # a filter on status = Complied.
        stem = re.sub(r"(?:ed|es|s|d)$", "", tokens[0])   # owned -> own
        return bool(stem) and re.search(
            rf"\b{re.escape(stem)}(?:e|es|ed|ing|s)?\b", low) is not None
    hits = sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}", low))
    return hits == len(tokens)


def _names_family(question: str, value: str) -> bool:
    """Does the question name the *family* this value belongs to?

    `ISO 9001:2015` is a family word plus a version; a bid desk asks "how many
    ISO certifications", naming the family and none of the numbers. Only fires
    when every token after the first is non-alphabetic, so "Kalinga National
    Bank" still needs all three words.
    """
    tokens = [t for t in re.split(r"\W+", value) if t]
    if len(tokens) < 2 or len(tokens[0]) < 3:
        return False
    if any(t.isalpha() for t in tokens[1:]):
        return False
    return re.search(rf"\b{re.escape(tokens[0])}\b", question, re.I) is not None


# "On lease rather than owned outright" names two values and selects one. The
# clause after the marker is the thing being contrasted *against*, not a second
# selection. "other than" is left out on purpose: it is an exclusion, and
# answering it as a selection would invert the question.
_CONTRAST = r"\brather than\b|\bas opposed to\b|\binstead of\b|\band not\b"


# A short status word carrying its ordinary English sense rather than naming a
# stored value. "Arunodaya Infrastructure, **submission due**, what's the
# outstanding balance?" filtered the ledger to `status='due'` and answered
# about 7 invoices out of 37 — the writer was stating a deadline, not a filter.
#
# The containment rule again, in the one direction the guards did not cover: not
# a value inside a *name*, but a value inside an ordinary **phrase**. Scoped to
# the word immediately before, because that is what distinguishes "submission
# due" from "the amount still due" — and kept to a short list, since a generic
# "ignore one-word values" would disable the mechanism this project measured.
_VALUE_AS_ENGLISH = re.compile(
    r"\b(?:submission|deadline|report|bid|tender|proposal|filing|deliverable)\s+"
    r"(?:is\s+)?due\b", re.I)


def _match_values(values: list[str], question: str) -> list[str]:
    # Blanked rather than deleted, so every other offset in the question — the
    # measure and operation spans are read by character position — is unmoved.
    question = _VALUE_AS_ENGLISH.sub(lambda m: " " * len(m.group(0)), question)
    named = [v for v in values if _mentions(question, v)]
    if not named:
        named = [v for v in values if _names_family(question, v)]
    return named


# "Anything other than an excavator", "clients apart from X". This EXCLUDES the
# value it names. `_CONTRAST` deliberately left "other than" out, with a comment
# saying that reading it as a selection would invert the question — which is
# exactly what leaving it out caused, because an unrecognised exclusion still
# matches the value and then selects it (`experiment_log.md` §19, `PG-11`,
# `PG-18`). Recognising it is what makes the comment's intent true.
# **THE ONE DEFINITION.** `answer.py` binds its `_EXCLUDE_MARKER` to this name
# rather than restating it. Its copy carried a comment reading "kept
# deliberately identical to `domains._EXCLUDE_MARKER`" — and by the time anyone
# looked, this list had **7 alternatives against that one's 13**. The comment
# asserted an invariant that nothing enforced, which is `decision_log.md` §22's
# defect returning as its own documentation. Two copies of a rule drift; one
# definition cannot (`CLAUDE.md`: "these two places should be one function").
# **THE ONE DEFINITION, for the same reason as `_EXCLUDE_MARKER` below.** Both
# of this module's average sites and `answer.py`'s bind to this name. They had
# drifted — here `typical`, there `typical size` — and the narrower copy sent
# "the typical project **scale**" to a portfolio sum, which scores 0 against a
# mean (`experiment_log.md` §54). Same defect shape as §47, one family later.
_AVERAGE_WORDS = r"average|mean|typical|avg"

_EXCLUDE_MARKER = (r"\bexcluding\b|\bother than\b|\bapart from\b|\bexcept(?:\s+for)?\b|"
                   r"\bbut not\b|\baside from\b|\bnot counting\b|\bleaving out\b|"
                   # "mahanadi steel corp, **minus** the water treatment side"
                   # and "when I start with the portfolio and **exclude** the
                   # bridges and flyovers" both *selected* the named category
                   # instead of removing it — the exact inversion this marker
                   # list exists to prevent (`experiment_log.md` §30).
                   r"\bminus\b|\bexclude[sd]?\b|\bless the\b|\bnet of\b|"
                   r"\btaking out\b|\bstripping out\b|\bwithout the\b|"
                   r"\bignoring\b|\bset(?:ting)? aside\b|\bdiscount(?:ing)? the\b|"
                   # §30's class, third occurrence. Three more spellings of the
                   # same instruction, each of which made the engine return
                   # **the excluded portion itself**: "once we **filter out**
                   # the industrial epc work", "after **dropping** the
                   # industrial epc work", "once we **remove** the water
                   # treatment segment". `_EXCLUDE_AFTER` knew the participle
                   # `removed` but nothing knew the bare verb.
                   # `filter out` and not `filter`: "could you actually
                   # **filter for** the heavier commitments" is a SELECTION,
                   # and widening to the bare verb would invert it.
                   r"\bfilter(?:s|ing|ed)? out\b|\bdropping\b|\bdrop the\b|"
                   r"\bremov(?:e|es|ing)\b")


def _named_values(records: list[dict], field: str, question: str) -> set[str]:
    """Every stored value the question names — a set, not the single best match.

    A question can name a family that spans rows ("ISO certifications" covers
    three standards); picking one would answer about a third of them. Where two
    matched values nest, the more specific one wins, which is what the old
    single-pick did.
    """
    values = _distinct(records, field)
    named = _match_values(values, question)
    if len(named) > 1 and (m := re.search(_CONTRAST, question, re.I)):
        if head := _match_values(values, question[:m.start()]):
            named = head
    return {v for v in named
            if not any(other != v and v.lower() in other.lower() for other in named)}


def _excluded_values(records: list[dict], field: str, question: str) -> set[str]:
    """The values named *after* an exclusion marker, which must be filtered
    out rather than selected. Only the clause following the marker is read, so
    "excavators other than the leased ones" excludes the right half alone.
    """
    if not (m := re.search(_EXCLUDE_MARKER, question, re.I)):
        return set()
    tail = question[m.end():]
    return {v for v in _match_values(_distinct(records, field), tail)}


# The works table's own text fields, for the same reason `WORKS_FIELDS` excludes
# their *names* from `DISTINCTIVE_FIELDS`: a dimension value that also occurs
# here (a client, a category, an engineer's name) must never be allowed to
# route on its own, however unique it looks within one domain.
_WORKS_TEXT_FIELDS = ("work", "client", "category", "grading", "role", "project_lead")


def _all_text_values(facts: dict) -> list[tuple[str, str]]:
    """(value, owning block) for every free-text field in the estate, `works`
    included under that name. The universe a candidate routing value is
    checked against, both for exact ownership and for containment.
    """
    out = []
    for w in facts.get("works", []):
        for f in _WORKS_TEXT_FIELDS:
            if v := w.get(f):
                out.append((str(v), "works"))
    for d in DOMAINS:
        for dim in d.dims:
            for v in _distinct(facts.get(d.block) or [], dim):
                out.append((v, d.block))
    return out


def _value_owners(universe: list[tuple[str, str]]) -> dict[str, set[str]]:
    """Every value in `universe`, folded to lowercase, mapped to every block
    that states it exactly. A value owned by more than one block can never
    vote (the module docstring's rule).

    Exact-match ownership alone is not enough, and is only the cheap first
    filter: "Gujarat" owns no other *exact* value, yet it is a substring of a
    dozen client names ("Jal Nigam, Gujarat"), which is exactly as unsafe as an
    exact duplicate. `_value_evidence` re-checks any single-owner candidate by
    containment against the same universe before trusting it.
    """
    owners: dict[str, set[str]] = collections.defaultdict(set)
    for v, block in universe:
        owners[v.lower()].add(block)
    return owners


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Does `haystack` literally contain `needle` as a whole phrase?

    Length floor and word boundaries, because `staff.wage_group` values are
    single letters and a bare `"b" in "tunnel boring machine"` disqualified a
    perfectly distinctive asset type (`experiment_log.md` §25). Same 3-char
    guard `_mentions` uses, for the same reason.
    """
    n = needle.lower()
    return len(n) > 3 and re.search(rf"\b{re.escape(n)}\b", haystack) is not None


def _value_evidence(question: str, d: Domain, facts: dict,
                    owners: dict[str, set[str]], universe: list[tuple[str, str]]) -> int:
    """Score a dimension value the question names, if — and only if — that
    value cannot plausibly refer to anything outside this block.

    "Tunnel Boring Machine" is shared with nothing, so naming it is as strong a
    signal as naming the block's own vocabulary ("plant register"); a client
    name, category or state name is shared with `works` (or contained inside
    one of its values) by construction and never qualifies. Scored like a
    `strong` marker — one distinctive value is enough to route on its own —
    because the risk `strong` markers exist to avoid (stealing a works
    question) is exactly what the ownership and containment checks rule out.

    Matched by *contiguous* substring only — `_mentions`'s looser token-scatter
    branch (all of a value's words present somewhere in the question, in any
    order) is fine for filtering an already-routed block, but not for routing
    itself: "Irrigation Works" scattered-matched a question about "Irrigation &
    Waterways Dept ... value of works" and stole a works question. A value that
    is only ever named as a whole phrase does not have that failure mode.
    """
    q = question.lower()
    for dim in d.dims:
        for v in _distinct(facts.get(d.block) or [], dim):
            vl = v.lower()
            # Word-boundary, not bare substring: `condition=fair` is contained
            # in "I'm **fair**ly certain", which routed a works question to the
            # 210-row asset register and summed all of it (`experiment_log.md`
            # §25). This is the containment rule `CLAUDE.md` warns about, at
            # word level — the block-name and client-name cases were fixed
            # before, and this is the same hazard on a dimension *value*.
            # A trailing plural is still the same value ("tunnel boring
            # machines" names `Tunnel Boring Machine`), but an arbitrary
            # continuation is not ("fairly" is not `fair`).
            if len(vl) <= 3 or not re.search(rf"\b{re.escape(vl)}(?:e?s)?\b", q):
                continue
            if owners.get(vl) != {d.block}:
                continue
            # Containment, BOTH ways. The one-directional test asked only
            # whether some other block's value names this one; bond
            # `work_description`s are systematically "<Category> Works", so
            # "Sewerage Drainage Works" contains the works category `sewerage
            # drainage` and routed a works question to the bond register
            # (`experiment_log.md` §25). A value that swallows another block's
            # vocabulary is exactly as ambiguous as one swallowed by it.
            # Reverse direction uses *literal phrase* containment, not
            # `_mentions`' stemming: "Sewerage Drainage Works" literally
            # contains the works category "sewerage drainage" and must not
            # route, while "Tunnel Boring Machine" merely stem-matches
            # "tunnels" and must still route (`PC-12`).
            if any(block != d.block
                   and (_mentions(text, v) or _contains_phrase(vl, text))
                   for text, block in universe):
                continue
            return ROUTE_FLOOR
    return 0


def parse_fy(question: str) -> str | None:
    """`FY 2019-20`, `2019-20`, `FY2020`, "year ended 31 March 2020" -> "2019-20"."""
    if m := re.search(r"\b(?:fy\s*)?(20\d{2})\s*[-/–]\s*(\d{2}|20\d{2})\b", question, re.I):
        return f"{m.group(1)}-{m.group(2)[-2:]}"
    # "year ended 31 March 2020" / "as at 31st March 2020" — the day number sits
    # between the phrase and the year, so this cannot be a non-digit run.
    # "the year TO 31 March 2023" is the same year the corpus writes as "year
    # ended 31 March 2023" (`experiment_log.md` §19, `PG-08`).
    if m := re.search(r"(?:year end\w*|ending|year to|up to|as (?:at|on))\D{0,12}(?:\d{1,2}\D{0,6})?"
                      r"(?:march|mar)\D{0,6}(20\d{2})", question, re.I):
        end = int(m.group(1))
        return f"{end - 1}-{str(end)[-2:]}"
    if m := re.search(r"\bfy\s*(20\d{2})\b", question, re.I):
        start = int(m.group(1))
        return f"{start}-{str(start + 1)[-2:]}"
    return None


# ORDER IS LOAD-BEARING. `parse_bound` returns on the first pattern that
# matches, and several of these contain each other: "no more than" contains
# "more than", "not exceeding" contains "exceed". With the positive forms
# first, every negated ceiling parsed as its own opposite and returned a
# confident wrong number (`experiment_log.md` §19, `PG-01`/`PG-02`). The
# ceilings and floors that carry a negation must therefore be tested *before*
# the bare comparatives they contain. Do not reorder without re-reading that.
_BOUNDS = (
    (r"(?:at most|no more than|not more than|no greater than|not greater than|"
     r"not exceeding|no higher than|not above)\s+", "le"),
    (r"(?:at least|no less than|not less than|minimum of|not below|no lower than)\s+", "ge"),
    (r"(?:less than|under|below|fewer than)\s+", "lt"),
    (r"(?:more than|greater than|over|above|exceed\w*|in excess of)\s+", "gt"),
)
# The same bounds said the other way round, with the number *first*: "20 years
# or more", "INR 10 Cr or less". `answer.py` has understood this since §3
# (`AT_OR_ABOVE`); this parser never did, which is `PD-10` and was confirmed
# twice more by `PG-03`/`PG-04`.
_SUFFIX_BOUNDS = (
    (r"\s*(?:or|and)\s+(?:more|above|higher|greater|over)\b", "ge"),
    (r"\s*(?:or|and)\s+(?:less|lower|below|fewer|under)\b", "le"),
)
_COMPARE = {"gt": lambda v, b: v > b, "ge": lambda v, b: v >= b,
            "lt": lambda v, b: v < b, "le": lambda v, b: v <= b}


def parse_bound(question: str) -> tuple[str, float] | None:
    """"more than 20 years" -> ("gt", 20); "at least INR 5 Cr" -> ("ge", 5e7).

    Only fires behind an explicit comparison phrase, so a bare year in "issued
    after 2020" or "the last three years" is never mistaken for a bar.
    """
    from money import parse_money

    for pattern, op in _BOUNDS:
        if not (m := re.search(pattern, question, re.I)):
            continue
        rest = question[m.end():]
        if mm := re.match(r"(?:INR|Rs\.?|₹)?\s*([\d,.]+)\s*(crore|cr\b|lakhs?|lacs?)", rest, re.I):
            return op, float(parse_money(f"{mm.group(1)} {mm.group(2)}"))
        if mm := re.match(r"(?:INR|Rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)", rest):
            return op, float(mm.group(1).replace(",", ""))
        # The comparison phrase is there but the number is spelled out —
        # "more than twenty years". Handled after the digit forms so a digit
        # always wins where both are present.
        if (n := _spelled_number(rest)) is not None:
            return op, float(n)

    # The bound said with the number first: "20 years or more".
    for pattern, op in _SUFFIX_BOUNDS:
        for m in re.finditer(pattern, question, re.I):
            head = question[:m.start()]
            # The last figure before the marker, not one anchored to the end of
            # the clause: "20 years of experience or more" puts three words
            # between the number and "or more".
            money = list(re.finditer(
                r"(?:INR|Rs\.?|₹)?\s*([\d,.]+)\s*(crore|cr\b|lakhs?|lacs?)", head, re.I))
            if money:
                mm = money[-1]
                return op, float(parse_money(f"{mm.group(1)} {mm.group(2)}"))
            plain = list(re.finditer(r"\b([\d,]+(?:\.\d+)?)\b", head))
            if plain:
                return op, float(plain[-1].group(1).replace(",", ""))
            if (n := _spelled_number(head[::-1][:40][::-1])) is not None:
                return op, float(n)
    return None


def _spelled_number(text: str) -> int | None:
    """"twenty years" -> 20, reading only the run of number words that starts
    the fragment. `answer.py` has had this since §3; this parser was digits
    only, so "more than twenty years" silently applied no bound at all."""
    words = re.findall(r"[a-z]+(?:-[a-z]+)?", text.lower()[:40])
    total = current = 0
    seen = False
    for word in words:
        parts = word.split("-")
        if not all(p in _NUMBER_WORDS for p in parts):
            break
        for p in parts:
            value = _NUMBER_WORDS[p]
            if value == 100:
                current = max(current, 1) * 100
            elif value >= 1000:
                total += max(current, 1) * value
                current = 0
            else:
                current += value
            seen = True
    return total + current if seen else None


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "thousand": 1000,
}


def apply_filters(records: list[dict], question: str, d: Domain, trace: dict,
                  measure: str | None = None) -> list[dict]:
    # A dimension value carrying its ordinary English sense is blanked before
    # any filter reads the question — see `_VALUE_AS_ENGLISH`. It has to happen
    # here as well as in `_match_values`, because `dim_synonyms` is tried
    # *first* and matches the raw text directly: blanking one path and not the
    # other left the defect exactly where it was, which is this project's most
    # repeated mistake (`decision_log.md` §18, §19, §24, §25).
    question = _VALUE_AS_ENGLISH.sub(lambda m: " " * len(m.group(0)), question)
    q = question.lower()

    # A bar on the measure the question is about: "bonds over INR 5 Cr",
    # "staff with more than 20 years of experience".
    if measure and (bound := parse_bound(question)):
        op, bar = bound
        trace["bound"] = f"{measure} {op} {bar:,.0f}"
        cmp = _COMPARE[op]
        records = [r for r in records
                   if isinstance(r.get(measure), (int, float)) and cmp(r[measure], bar)]

    if d.fy_field and (fy := parse_fy(question)):
        trace["fy"] = fy
        records = [r for r in records if r.get(d.fy_field) == fy]
    elif d.fy_field and records:
        # No year named. Summing a balance-sheet item across seven statements
        # is meaningless (net worth is a stock, not a flow), and summing
        # turnover silently answers a different question, so the default is the
        # latest year on file. A multi-year cue overrides it, and
        # "last N years" — how every tender turnover gate is written — keeps N.
        years = sorted({r[d.fy_field] for r in records if r.get(d.fy_field)})
        if m := re.search(r"last\s+(\d+|three|five|two|four)\s+(?:financial\s+|fiscal\s+)?years?", q):
            word = m.group(1)
            n = int(word) if word.isdigit() else {"two": 2, "three": 3, "four": 4, "five": 5}[word]
            keep = set(years[-n:])
            trace["fy_window"] = sorted(keep)
            records = [r for r in records if r.get(d.fy_field) in keep]
        elif not re.search(r"each year|every year|across all year|over the years|"
                           r"all seven|year on year|cumulative|combined across", q):
            trace["fy_default"] = years[-1]
            records = [r for r in records if r.get(d.fy_field) == years[-1]]

    # "Contract 74" names a key, not a category: its value is a bare number, and
    # the value matcher ignores short numeric tokens on purpose.
    for field in d.key_fields:
        name = field.replace("_", r"[ _]?")
        if m := re.search(rf"\b{name}\s*(?:no\.?|number|#)?\s*(\d+)\b", q):
            key = int(m.group(1))
            trace.setdefault("keys", {})[field] = key
            records = [r for r in records if r.get(field) == key]

    # The word that picked the measure must not also drive a filter. "Total
    # outstanding across the ledger" names the column; reading it a second time
    # as a status narrowed the ledger to the rows already labelled due.
    _, measure_span = named_measure(question, d)
    for field in d.dims:
        synonyms = d.dim_synonyms.get(field, {})
        chosen = {v for v, pat in synonyms.items()
                  if (m := re.search(pat, q, re.I)) and not _overlaps(m.span(), measure_span)}
        if not chosen:
            # The same guard, on the fallback path. It was applied only to the
            # synonym branch, so a measure word that is *also a stored value*
            # slipped straight past it: "the total amount currently **due**"
            # named the `outstanding` measure and was then read a second time as
            # `status='due'`, narrowing five invoices to two. It looked correct
            # on the neighbouring question only by data accident -- that client's
            # invoices are all `paid`, so there was no `due` value for the
            # fallback to find (`experiment_log.md` §38).
            chosen = _named_values(records, field, _blank(question, measure_span))
        # An exclusion names its value the same way a selection does, so it has
        # to be recognised before the value is used, not after: "anything other
        # than excavators" otherwise selects exactly the excavators.
        if excluded := _excluded_values(records, field, question):
            trace.setdefault("dims", {})[field] = "not " + "/".join(sorted(excluded))
            records = [r for r in records if str(r.get(field)) not in excluded]
            continue
        if not chosen:
            continue
        negated = bool((pat := d.negate.get(field)) and re.search(pat, q, re.I))
        trace.setdefault("dims", {})[field] = ("not " if negated else "") + "/".join(sorted(chosen))
        # Where the question named this value. A word *inside* a value's own
        # text must not also drive the operation — "minimum AVERAGE turnover"
        # is the name of a tender gate, not a request for a mean
        # (`experiment_log.md` §19; the same one-word-two-jobs class as the
        # measure guard below and `PG-06`'s "at least").
        for value in chosen:
            if vm := re.search(re.escape(value.lower()), q):
                trace.setdefault("_value_spans", []).append(vm.span())
        records = [r for r in records if (str(r.get(field)) in chosen) is not negated]

    for flag in d.flags:
        term = flag.replace("_", " ")
        if re.search(rf"\b{term}\b|\b{term.split()[0]}\b", q):
            want = not re.search(r"\bnot\b|\bno\b|without|un(?:certified|safe)|lack", q)
            trace.setdefault("flags", {})[flag] = want
            records = [r for r in records if bool(r.get(flag)) is want]

    if d.year_field and (m := re.search(r"\b(?:since|after|from)\s+(20\d{2})\b", q)):
        trace["year_after"] = int(m.group(1))
        records = [r for r in records if (r.get(d.year_field) or 0) >= int(m.group(1))]
    elif d.year_field and (m := re.search(r"\b(?:before|prior to|up to)\s+(20\d{2})\b", q)):
        trace["year_before"] = int(m.group(1))
        records = [r for r in records if (r.get(d.year_field) or 0) < int(m.group(1))]

    # A bond or certificate is live or lapsed by its expiry, not by a status
    # word — 32 of 60 bonds print no status at all, so a status filter would
    # silently drop them. But "due to expire before 2029" is a different
    # question from "has already expired" — the word "expire" alone would
    # otherwise answer against the wall-clock live/lapsed split every time,
    # even when an explicit future date bound is what was actually asked for.
    if (d.expiry_field and (state := _validity_state(q))
            and not re.search(rf"\b(?:before|after|prior to|since|from|"
                              rf"earlier than|later than)\s+(?:{_DATEISH})", q)):
        asof = _asof(question)
        trace["validity"] = f"{state} as of {asof}"
        records = [r for r in records
                   if r.get(d.expiry_field)
                   and ((r[d.expiry_field] >= asof) == (state == "live"))]

    # A block with more than one date field (`certifications`: issued *and*
    # expiry) applies a bare "before/after <year>" bound to whichever field is
    # first in the tuple and stops there — right when there is only one field
    # to choose from, wrong when the question's own wording names the other
    # one ("due to expire before 2029" is about `valid_until`, not
    # `initial_date`, even though the latter is listed first).
    date_fields = d.date_fields
    if d.expiry_field and re.search(r"expir\w+|lapsed", q) and d.expiry_field in date_fields:
        date_fields = (d.expiry_field,) + tuple(f for f in date_fields if f != d.expiry_field)

    for field in date_fields:
        if field == d.expiry_field and trace.get("validity"):
            continue
        # A bare year bounds the whole year: "issued after 2020" excludes every
        # 2020 date, "joined before 2015" excludes every 2015 date. Comparing
        # against the bare "2020" would instead keep 2020-05-01, since
        # "2020-05-01" > "2020" as strings.
        if m := re.search(rf"\b(?:since|after|from|later than)\s+(?:{_DATEISH})", q):
            bound = f"{m.group(1)}-12-31"
            trace[f"{field}_after"] = bound
            records = [r for r in records if str(r.get(field) or "") > bound]
        elif m := re.search(rf"\b(?:before|prior to|up to|earlier than)\s+(?:{_DATEISH})", q):
            bound = f"{m.group(1)}-01-01"
            trace[f"{field}_before"] = bound
            records = [r for r in records if str(r.get(field) or "") and str(r[field]) < bound]
        elif not d.fy_field and (m := re.search(
                r"\b(?:in|during|for|dated|dated in|raised in|issued in)\s+(20\d{2})\b", q)):
            trace[f"{field}_year"] = m.group(1)
            records = [r for r in records if str(r.get(field) or "").startswith(m.group(1))]
        else:
            continue
        break
    return records


# A bare year in a date comparison means its first instant: "joined before 2015"
# excludes everyone who joined during 2015.
_DATEISH = r"(20\d{2})"


def _validity_state(q: str) -> str | None:
    """Live or lapsed, by expiry date.

    ORDER IS LOAD-BEARING, for the same reason `_BOUNDS`' is: "not yet
    expired" contains "expired", so testing the lapsed pattern first answered
    the exact opposite question (`experiment_log.md` §19, `PG-09`). The
    negated forms are therefore tested first, and the lapsed pattern refuses a
    negation immediately in front of it.
    """
    if re.search(r"not (?:yet )?(?:expired|lapsed)|still (?:live|valid|open|in force|running)|"
                 r"\blive\b|currently (?:valid|active)|in force|outstanding bond|"
                 r"yet to expire|have not expired", q):
        return "live"
    if re.search(r"(?<!not )(?<!yet )\b(?:expir\w+|lapsed)\b|"
                 r"no longer (?:valid|live|in force)|closed out", q):
        return "lapsed"
    return None


def _asof(question: str) -> str:
    """The date liveness is judged against. A question that names one wins;
    otherwise the corpus's own horizon, so the answer does not drift with the
    wall clock and stays reproducible."""
    if m := re.search(r"as (?:at|of)\s+(\d{4})-(\d{2})-(\d{2})", question, re.I):
        return m.group(0).split()[-1]
    if m := re.search(r"as (?:at|of)[^\d]{0,12}(20\d{2})", question, re.I):
        return f"{m.group(1)}-03-31"
    return ASOF


# The latest fiscal-year end the corpus covers (ledger runs to 2026-27).
ASOF = "2026-03-31"


def named_measure(question: str, d: Domain) -> tuple[str | None, tuple[int, int] | None]:
    """The measure the question *names*, and where it named it. `None` means the
    question named none, which is different from falling back to the default —
    a share, for one, is by record count until a measure is actually asked for.

    Longest synonym match wins, so "profit after tax" beats a bare "tax" and
    "net worth" beats "worth".
    """
    best, span, best_len = None, None, 0
    for field, pattern in [(f, p) for f, (p, _) in d.derived.items()] + list(d.measures.items()):
        for m in re.finditer(pattern, question, re.I):
            if len(m.group(0)) > best_len:
                best, span, best_len = field, m.span(), len(m.group(0))
    return best, span


def pick_measure(question: str, d: Domain) -> str | None:
    """The numeric column the question asks about, falling back to the block's
    default when it names none."""
    return named_measure(question, d)[0] or d.default_measure


def named_measures(question: str, d: Domain) -> list[str]:
    """Every distinct measure field the question names, in the order it first
    names each one — `named_measure` returns only the single best guess, which
    is right for picking *the* measure but cannot see a question that names
    two ("what percentage of what we have invoiced has actually been
    collected"). Used only by the ratio/difference mechanism below.
    """
    hits = []
    for field, pattern in [(f, p) for f, (p, _) in d.derived.items()] + list(d.measures.items()):
        if m := re.search(pattern, question, re.I):
            hits.append((m.start(), m.end(), field))
    # "Profit after tax" names `pat`; the `tax` measure's own pattern also
    # matches, *inside that same span*. A measure matched strictly within
    # another's match is a fragment of it, not a second measure named — and
    # the ratio below only fires on exactly two, so an uncounted fragment
    # silently disables the whole mechanism rather than skewing it. Same
    # span-overlap reasoning §14 applied between a measure and an operator,
    # here between two measures.
    hits = [h for h in hits
            if not any(o[0] <= h[0] and h[1] <= o[1] and o[1] - o[0] > h[1] - h[0]
                       for o in hits)]
    return [field for _, _, field in sorted(hits)]


# "How much MORE have we invoiced X THAN we have received" — a subtraction
# between two measures, not a bound (`more than 20 years`): those always pair
# "more than" adjacently, immediately before a number, which this does not.
# "Exceed" is the same question in one word ("by how much did expenses exceed
# profit"), and carries its own direction.
_MEASURE_DIFFERENCE = (r"how much (?:more|less|greater|smaller|higher|lower)\b|"
                       r"difference between|\bexceed\w*\b|\bshort of\b")
# Which way round the subtraction runs. "How much more have we invoiced than
# received" is invoiced − received, but "how much **less** have we received
# than invoiced" is the same positive shortfall with the measures named in the
# opposite order — first-minus-second would answer it with a negative number,
# which is not an answer to the question asked. "Difference between" states no
# direction at all and is a magnitude.
_DIFFERENCE_REVERSED = r"how much (?:less|smaller|lower)\b|\bshort of\b"
_DIFFERENCE_MAGNITUDE = r"difference between"
# A ratio between two of a block's own measures, not the works-table
# `%`-vs-population share this word set also drives — the two are told apart
# by how many measures the question names (`aggregate`'s call site).
_RATIO_WORD = r"\bpercent\b|percentage|proportion|\bshare\b|\bfraction\b|%|out of one hundred"
# "Realisation rate", "collection rate" and "recovery rate" are standard bid-
# desk names for one measure *as a share of the block's own default measure*
# — the idiom names only the numerator, so the denominator has to come from
# somewhere else. Deliberately not a bare "rate": `boq_items` stores a `rate`
# *column*, and a question asking for it must not be read as a ratio.
_IMPLICIT_RATE = r"realisation rate|realization rate|collection rate|recovery rate"


def with_derived(records: list[dict], d: Domain) -> list[dict]:
    """Materialise the derived measures onto each record. A record missing any
    contributing field yields None, not a partial sum — a net worth computed
    from half a balance sheet is worse than no answer."""
    if not d.derived:
        return records
    out = []
    for r in records:
        extra = {}
        for name, (_, parts) in d.derived.items():
            vals = [r.get(f) for f in parts]
            extra[name] = (sum(r[f] * c for f, c in parts.items())
                           if all(isinstance(v, (int, float)) for v in vals) else None)
        out.append({**r, **extra})
    return out


# The word that decides how per-group values *combine* — not the same set
# `aggregate`'s ungrouped max/min admit, because a group-by question says
# "the most"/"the least" bare far more often than "largest"/"smallest" ("the
# client we have billed the most"), and that bare form is safe here only
# because it is gated on a dim already having matched generically.
_GROUP_MAX = r"\blargest\b|\bbiggest\b|\bmaximum\b|\bhighest\b|\bmost\b|\btop\b"
# `(?<!at )` so "at least N" — a floor on the measure — cannot be read as a
# request for the minimum of it (`experiment_log.md` §19, `PG-06`).
_GROUP_MIN = r"\bsmallest\b|\bminimum\b|\blowest\b|(?<!at )\bleast\b"
_GROUP_COMBINE = _GROUP_MAX + r"|" + _GROUP_MIN + r"|\baverage\b|\bmean\b"
# A superlative is *inherently* per-group — "the client we have billed the
# most" can only mean one client, so naming the dim generically is the whole
# signal (PC-06, PC-07). An average is not: "the average invoice amount across
# all our clients" averages **invoices**, while "the average amount we invoice
# each client" averages **clients**, and the two differ by a factor of 20 on
# this corpus (518 invoices, 25 clients). So an average has to *say* it is
# per-group before the dim noun alone is allowed to group on it. Up to two
# words may sit between the marker and the noun ("each business unit").
_PER_GROUP = r"(?:each|every|per|apiece|any (?:one|single))\s+(?:\w+\s+){0,2}"


def _reduce_one(records: list[dict], question: str, d: Domain, field: str | None,
                outer_average: bool = False):
    """One group's value in a group-by — sum, count or average, by the same
    triggers `aggregate` uses for a whole (ungrouped) answer. Never max/min:
    those are consumed by the group-by's own *outer* combination, and reusing
    them here would take the single largest record inside every group instead
    of reducing the group to one number per group.

    `outer_average` extends that same rule to the average, which the caller
    sets when the average is what combines the groups. "The average amount we
    invoice each client" is each client's *total*, averaged across clients —
    averaging inside the group as well returns a mean of per-client means,
    which is neither that nor the flat mean over invoices, and is wrong under
    every reading rather than merely arguable. It stayed invisible because the
    only grouped probes that named an average had one record per group
    (`business_units`; `trial_balance` once filtered to one FY), where a mean
    of means is arithmetically the same number.
    """
    q = question.lower()
    values = [r[field] for r in records if isinstance(r.get(field), (int, float))] if field else []
    if not outer_average and re.search(_AVERAGE_WORDS, q):
        return int(sum(values) / len(values)) if values else 0
    if re.search(d.counts, q) and not re.search(r"how much", q):
        if d.count_measure:
            return sum(r[d.count_measure] for r in records
                       if isinstance(r.get(d.count_measure), (int, float)))
        return len(records)
    return sum(values)


def aggregate(records: list[dict], question: str, d: Domain, trace: dict,
              population: list[dict] | None = None):
    """`population` is the block before FILTER ran — the denominator a share
    needs. Without it a share is 100% by construction."""
    q = question.lower()
    field = pick_measure(question, d)
    trace["measure"] = field
    values = [r[field] for r in records if isinstance(r.get(field), (int, float))] if field else []

    # The word that picked the measure must not also drive the op: "paid-up
    # SHARE CAPITAL" names `paid_up_capital` via the synonym "share capital",
    # and re-scanning the whole question for "share" without excluding that
    # span reads it a second time as the percentage operator.
    _, measure_span = named_measure(question, d)
    # Spans the question spent naming a dimension value, recorded by
    # `apply_filters`. An operation word falling inside one of them belongs to
    # the value's name, not to the question's verb.
    value_spans = trace.get("_value_spans") or []

    def _asks(pattern: str):
        """First match of `pattern` that is not inside the measure's name or
        inside a named value's name."""
        for m in re.finditer(pattern, q):
            # Deliberately NOT excluded by `measure_span`: "costliest" contains
            # the `cost` measure's own pattern, and excluding it turned a max
            # into a sum (caught by PB-03 while building this). Only a value's
            # own name suppresses an operation word.
            if any(m.start() >= s and m.end() <= e for s, e in value_spans):
                continue
            return m
        return None

    if re.search(r"how many (?:different|distinct|unique|separate|individual)\b|"
                 r"distinct|unique|how many (?:separate )?kinds", q):
        for dim in d.dims:
            # The column name, or the noun a question would use for it —
            # "how many separate tenders" has to reach `tender_ref`.
            noun = d.dim_nouns.get(dim) or rf"\b{dim.replace('_', ' ')}s?\b"
            if re.search(noun, q, re.I):
                trace["op"] = f"distinct_{dim}"
                return len({r.get(dim) for r in records if r.get(dim) not in (None, "")})
        trace["op"] = "distinct_first_dim"
        dim = d.dims[0] if d.dims else None
        return len({r.get(dim) for r in records if r.get(dim) not in (None, "")}) if dim else len(records)

    # A ratio or difference between two of a block's own measures — "what
    # percentage of what we have invoiced has actually been collected",
    # "realisation rate", "how much more have we invoiced than received" —
    # rather than the single-measure share below. `aggregate()` used to serve
    # exactly one measure per question; this widens it generically, so
    # billed-against-awarded on `contracts` costs nothing once invoiced-vs-
    # received on `invoices` exists (roadmap item 1).
    named = named_measures(question, d)
    implicit_rate = bool(re.search(_IMPLICIT_RATE, q))

    def _sum(f: str) -> int | float:
        return sum(r[f] for r in records if isinstance(r.get(f), (int, float)))

    if len(named) == 2 and re.search(_MEASURE_DIFFERENCE, q):
        first, second = (tuple(reversed(named)) if re.search(_DIFFERENCE_REVERSED, q) else named)
        trace["op"], trace["measures"] = "measure_difference", (first, second)
        gap = _sum(first) - _sum(second)
        return abs(gap) if re.search(_DIFFERENCE_MAGNITUDE, q) else gap

    # "Percentage OF X that HAS BEEN Y" names the whole first and the part
    # second, so the pair is used in reverse: X is the denominator. The
    # "X rate" idiom names only the numerator; the denominator is implicit —
    # the block's own default measure is what a rate is always taken against.
    ratio_pair = (tuple(reversed(named)) if len(named) == 2 else
                 (named[0], d.default_measure) if len(named) == 1 and implicit_rate else None)
    if ratio_pair and (implicit_rate or re.search(_RATIO_WORD, q)):
        numer_field, denom_field = ratio_pair
        denom = _sum(denom_field)
        trace["op"], trace["measures"] = "measure_ratio", ratio_pair
        return round(100 * _sum(numer_field) / denom, 2) if denom else 0

    # A group-by: a question can name one of the block's own dims *generically*
    # ("each state", "any single one of them", "the client we have billed the
    # most") rather than a specific value of it — the difference from an
    # ordinary filter is exactly that no specific value matches
    # (`_named_values`, already used by `apply_filters`, is empty). "which
    # client owes the most" is then a per-group reduction (sum/count/average,
    # same triggers `aggregate` already has) followed by a max/min/average
    # *across* the groups.
    # `key_fields` group as readily as `dims` do — "the highest total measured
    # on any single contract" groups `measurements` by the contract it keys on,
    # which is not a dimension of it. They are excluded from `dims` because a
    # key is not something to *filter by value* on; that says nothing about
    # grouping. Dims stay first so a real dimension wins a noun collision.
    for dim in d.dims + tuple(f for f in d.key_fields if f not in d.dims):
        noun = d.dim_nouns.get(dim) or rf"\b{dim.replace('_', ' ')}s?\b"
        if not (re.search(noun, q, re.I) and not _named_values(records, dim, question)):
            continue
        if not re.search(_GROUP_COMBINE, q):
            break  # names the dim generically but isn't a group-by question
        outer_max, outer_min = re.search(_GROUP_MAX, q), re.search(_GROUP_MIN, q)
        explicit = re.search(_PER_GROUP + rf"(?:{noun})", q, re.I)
        # An average groups only if the question asks for it per-group; a
        # superlative carries that on its own (`_PER_GROUP`) — but only over a
        # real dimension. A *key* field must be asked for explicitly whatever
        # the operator: "the largest single amount measured across any of our
        # contracts" ranks measurement lines and merely names contracts as the
        # scope, while "the highest total measured on any single contract"
        # ranks contracts. Both say "single"; only the second says per-group,
        # and grouping the first turns a record superlative into a group one.
        if not (explicit or ((outer_max or outer_min) and dim in d.dims)):
            break
        groups: dict = {}
        for r in records:
            key = r.get(dim)
            if key not in (None, ""):
                groups.setdefault(key, []).append(r)
        if not groups:
            break
        per_group = {k: _reduce_one(v, question, d, field,
                                    outer_average=not (outer_max or outer_min))
                     for k, v in groups.items()}
        trace["op"], trace["group_by"] = "group_by", dim
        if outer_max:
            return max(per_group.values())
        if outer_min:
            return min(per_group.values())
        return round(sum(per_group.values()) / len(per_group), 2)

    # A share is the filtered set against the set it was drawn from. By value
    # when a measure is in play ("what share of receivables is overdue"),
    # otherwise by record count ("what share of the fleet is owned").
    if next((m for m in re.finditer(r"percent|share|proportion|%|out of one hundred", q)
             if not _overlaps(m.span(), measure_span)), None):
        trace["op"] = "share"
        whole = population if population is not None else records
        # "What percentage of the fleet is on lease" is a count of machines; a
        # block's *default* measure must not silently turn it into a share of
        # rupees. Only a measure the question actually named — or an explicit
        # "by value" — makes it a share by value.
        by_value = bool(named_measure(question, d)[0]) or re.search(
            r"\bby (?:value|cost|amount|worth|rupee)", q)
        if field and values and by_value:
            denom = sum(r[field] for r in whole if isinstance(r.get(field), (int, float)))
            return round(100 * sum(values) / denom, 2) if denom else 0
        return round(100 * len(records) / len(whole), 2) if whole else 0

    # Before the average branch, for the same reason `answer.py` puts it there:
    # "the difference between the average and the median" contains "average",
    # and testing that first returns the mean. Carried across to this path in
    # the same change as the works path — the repo's standing rule after §19,
    # §20 and §22 each cost a defect to one path being fixed and not the other.
    if values and _asks(r"\bmedian\b"):
        mid = statistics.median(values)
        if _asks(r"\bmean\b|\baverage\b|\bavg\b"):
            trace["op"] = "mean_median_gap"
            return int(round(abs(statistics.fmean(values) - mid)))
        trace["op"] = "median"
        return int(round(mid))

    if _asks(_AVERAGE_WORDS):
        trace["op"] = "average"
        return int(sum(values) / len(values)) if values else 0

    if _asks(r"\blargest\b|\bbiggest\b|\bmaximum\b|\bhighest\b|\bmost expensive\b|\bcostliest\b"):
        trace["op"] = "max"
        return max(values) if values else 0

    if _asks(r"\bsmallest\b|\bminimum\b|\blowest\b|\bcheapest\b|(?<!at )\bleast\b"):
        trace["op"] = "min"
        return min(values) if values else 0

    if re.search(d.counts, q) and not re.search(r"how much", q):
        # "How many people do we employ" totals a headcount column; "how many
        # business units" counts rows.
        if d.count_measure and not (d.record_noun and re.search(d.record_noun, q, re.I)):
            trace["op"] = "count_via_measure"
            trace["measure"] = d.count_measure
            return sum(r[d.count_measure] for r in records
                       if isinstance(r.get(d.count_measure), (int, float)))
        trace["op"] = "count"
        return len(records)

    trace["op"] = "sum"
    return sum(values)


# Aggregations that are zero by construction rather than by evidence. A trial
# balance *balances*: summing the `balance` column over more than one account
# is identically 0 in all seven fiscal years (verified), so that number says
# nothing about the question — it is a fallback zero wearing a computed zero's
# clothes, the distinction `findings.md` §9 insists on keeping.
#
# Four evaluation questions hit exactly this: receivables questions whose word
# "balance" routed them here, each returning a guaranteed 0.0 against a nonzero
# gold (`experiment_log.md` §24). The vocabulary that misrouted them is fixed,
# but this guard is the part that generalises — it refuses the degenerate
# answer whatever phrasing arrives next.
# A filter that contradicts the measure it is filtering for.
#
# "run the adjusted balance after all the payments we've actually **cleared**"
# and "we flagged the account as **fully billed**" both set `status='paid'`,
# and the measure asked for is `outstanding`. A paid invoice is *by definition*
# not outstanding, so the sum came out **negative** — ₹-82,485,684 and
# ₹-67,275,729, two answers no receivable can take (`experiment_log.md` §29).
#
# The words are describing the *deduction the question wants performed*, not a
# subset of invoices to look at. This is the one-word-two-jobs class
# (`decision_log.md` §9(d)) with a new signature: the two jobs contradict each
# other, so no gold is needed to see it — same family as `is_degenerate`, which
# spots an aggregation that is zero by construction.
_CONTRADICTORY = {("outstanding", "paid"), ("received", "due"), ("outstanding", "cancelled")}


def _drop_contradictory_status(records, population, trace: dict, measure):
    """Matched on the status's *parts*, because a family match stores them
    joined: "deduct every cleared payment" resolved `status='due/paid'`, which
    no exact-pair table can see."""
    # `trace["measure"]` is not set until `aggregate` runs, so the measure is
    # passed in rather than read back out -- reading it here silently disabled
    # the whole guard on the first attempt.
    dims = trace.get("dims") or {}
    status = dims.get("status")
    parts = re.split(r"[/,|]", str(status).lower()) if status else []
    if status and any((measure, part.strip()) in _CONTRADICTORY for part in parts):
        trace["dropped_filter"] = f"status={status} contradicts measure={measure}"
        dims.pop("status", None)
        return [r for r in population
                if all(r.get(k) == v for k, v in dims.items())]
    return records


def is_degenerate(value, trace: dict, d: Domain) -> bool:
    return (value == 0
            and d.block == "trial_balance"
            and trace.get("measure") == "balance"
            and trace.get("op") == "sum"
            and trace.get("n", 0) > 1
            and "account" not in (trace.get("dims") or {}))


def answer(question: str, facts: dict, d: Domain,
           scope_hint: dict[str, str] | None = None) -> tuple[float | int, dict]:
    """`scope_hint` is a field/value the *caller* resolved and this path did not.

    Block filtering matches a dimension value by name, which is strictly weaker
    than `answer.Facts.find_client` — that resolver has understood initials
    ("pwd gujarat"), a distinctive single token and near-misses since batch 2,
    and the block path never got any of it. So "pwd gujarat, what's the actual
    remaining balance still on their end?" reached the right block, matched no
    client, and summed **all 518 invoices**; a second question about a different
    client returned the identical number (`experiment_log.md` §27).

    The hint is applied only where it is *safe and additive*: the field must
    exist on the block, the question's own filtering must not already have set
    it, and the value must actually occur. It can therefore narrow a query that
    was too wide but can never widen or redirect one.

    §18's rule, for the third time: when two paths do the same job, the one that
    missed the fixes is the older one. Here the fix is shared rather than
    duplicated.
    """
    trace: dict = {"block": d.block}
    population = with_derived(list(facts.get(d.block, [])), d)
    measure = pick_measure(question, d)
    records = apply_filters(population, question, d, trace, measure)
    records = _drop_contradictory_status(records, population, trace, measure)
    for field, value in (scope_hint or {}).items():
        if (trace.get("dims") or {}).get(field) is not None:
            continue
        if any(r.get(field) == value for r in population):
            records = [r for r in records if r.get(field) == value]
            trace.setdefault("dims", {})[field] = value
            trace["scope_hint"] = field
    trace["n"] = len(records)
    value = aggregate(records, question, d, trace, population)
    if is_degenerate(value, trace, d):
        trace["degenerate"] = f"{d.block}/{trace.get('measure')}/{trace.get('op')} is 0 by construction"
    return value, trace

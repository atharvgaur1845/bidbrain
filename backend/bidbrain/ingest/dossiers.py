"""Past bids from the 6 tender dossiers — BidBrain's episodic memory.

Each dossier is a submitted bid: header (tender, category, bid value, date,
client), Annexure D (works cited as past performance), Annexure E (people
proposed), Annexure H (bid security). Loaded as

    (:Bid)-[:FOR_TENDER]->(:Tender)  (:Bid)-[:TO_CLIENT]->(:Client)
    (:Bid)-[:CITES]->(:Work)  (:Bid)-[:PROPOSES]->(:Person)  (:Bid)-[:SECURED_BY]->(:Bond)

so a new tender can retrieve precedents ("last time we bid expressways for a
state client we cited these works and this PM") and a change to a cited work
ripples to every bid that relied on it.
"""
from __future__ import annotations

import re
import sys

from bidbrain import config
from bidbrain.ingest.aliases import norm

sys.path.insert(0, str(config.VENDOR / "src"))
import extract as X  # noqa: E402
from money import best_money  # noqa: E402


def parse_dossier(doc_id: str, text: str) -> dict:
    pages = text.split("\f")
    p0 = pages[0].splitlines()
    head = "\n".join(p0[:12])
    ref = re.search(r"RFP-\d+", head).group(0)
    cat = re.search(r"\n(.+?) Works — Tender", head)
    value = best_money(re.search(r"Bid value:\s*(.+)", head).group(1))
    submitted = X.parse_date(re.search(r"Submitted:\s*(.+)", head).group(1))
    client = None
    for i, ln in enumerate(p0):
        if ln.strip().startswith("The Tender Inviting Authority"):
            client = p0[i + 1].strip()
            break
    cited, people, security = [], [], None
    section = None
    for pg in pages:
        h = pg[:80].lower()
        if "annexure d —" in h or "annexure d -" in h:
            section = "D"
        elif "annexure d2" in h:
            section = "D2"
        elif "annexure e" in h:
            section = "E"
        elif "annexure f" in h or "annexure g" in h:
            section = None
        elif "annexure h" in h:
            section = "H"
        if section == "D":
            flat = " ".join(pg.split())
            cited += [int(n) for n in re.findall(r"Pkg-(\d+)", flat)]
        elif section == "E":
            people += re.findall(r"^([A-Z][a-z]+ [A-Z][a-z]+)\n(?:Project Manager|Site Engineer|Engineer|Manager)", pg, re.M)
        elif section == "H":
            m = re.search(r"Valid To\n(.+?)\n(.+?)\n(INR [^\n]+)\n([^\n]+)", pg)
            if m:
                security = {"instrument": m.group(1), "bank": m.group(2), "amount": best_money(m.group(3)),
                            "valid_to": X.parse_date(m.group(4))}
    return {"key": ref, "tender_ref": ref, "doc_id": doc_id, "category": cat.group(1).lower() if cat else None,
            "bid_value": value, "submitted": submitted, "client": client,
            "cited_pkgs": sorted(set(cited)), "people": sorted(set(people)), "security": security}


def load_bids(g, pages) -> list[dict]:
    bids = [parse_dossier(d, t) for d, t in sorted(pages.raw.items()) if d.startswith("DOC-DOSSIER-")]
    for b in bids:
        g.run("""MERGE (t:Tender {key:$ref}) ON CREATE SET t.ref = $ref, t.category = $cat
                 CREATE (b:Bid {key:$ref, tender_ref:$ref, category:$cat, bid_value:$val, submitted:$sub,
                                submitted_day:$sday, doc_id:$doc, outcome:'submitted'})
                 CREATE (b)-[:FOR_TENDER]->(t)
                 WITH b MATCH (d:Document {doc_id:$doc}) CREATE (b)-[:EVIDENCED_BY {kind:'tender_dossier', page:1}]->(d)
                 WITH b OPTIONAL MATCH (c:Client {key:$client})
                 FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | CREATE (b)-[:TO_CLIENT]->(c))""",
              {"ref": b["tender_ref"], "cat": b["category"], "val": b["bid_value"], "sub": b["submitted"],
               "sday": _day(b["submitted"]), "doc": b["doc_id"], "client": norm(b["client"] or "")})
        g.run("""MATCH (b:Bid {key:$ref}) UNWIND $pkgs AS n
                 MATCH (w:Work) WHERE w.title ENDS WITH ('Pkg-' + toString(n))
                 CREATE (b)-[:CITES {annexure:'D'}]->(w)""", {"ref": b["tender_ref"], "pkgs": b["cited_pkgs"]})
        g.run("""MATCH (b:Bid {key:$ref}) UNWIND $ppl AS nm
                 MATCH (p:Person {key:nm}) CREATE (b)-[:PROPOSES {annexure:'E'}]->(p)""",
              {"ref": b["tender_ref"], "ppl": [norm(p) for p in b["people"]]})
        if b["security"]:
            # the annexure prints the amount rounded to 0.01 Cr (Rs 1 lakh), so match within that precision,
            # preferring the bond that secures this bid's own tender over a same-bank bond on another one
            g.run("""MATCH (b:Bid {key:$ref}), (bd:Bond)-[:ISSUED_BY]->(:Bank {key:$bank})
                     WHERE abs(bd.amount - $amt) <= 50000
                     OPTIONAL MATCH (bd)-[own:SECURES]->(:Tender {key:$ref})
                     WITH b, bd, own IS NOT NULL AS own_tender, abs(bd.amount - $amt) AS gap
                     ORDER BY own_tender DESC, gap LIMIT 1
                     CREATE (b)-[:SECURED_BY]->(bd)""",
                  {"ref": b["tender_ref"], "bank": b["security"]["bank"], "amt": b["security"]["amount"]})
    return bids


def _day(iso):
    from bidbrain.graph.client import day
    return day(iso)

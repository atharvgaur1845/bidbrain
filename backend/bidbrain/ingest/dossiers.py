"""Past bids from the 6 tender dossiers — BidBrain's episodic memory.

Each dossier is a submitted bid: header (tender, category, bid value, date,
client), Annexure D (past performance), Annexure E (key personnel), Annexure H
(bid security). Loaded as

    (:Bid)-[:FOR_TENDER]->(:Tender)  (:Bid)-[:TO_CLIENT]->(:Client)
    (:Bid)-[:CITES]->(:Work)  (:Bid)-[:PROPOSES]->(:Person)  (:Bid)-[:SECURED_BY]->(:Bond)

so a new tender can retrieve precedents ("last time we bid expressways for a
state client we cited these works") and a change to a cited work ripples to
the bids that relied on it.

What the annexures name, and what becomes an edge:

* Annexure D is titled "Past performance (<category>)" and prints 48 rows. The
  first 10-14 are the company's works in the tender's category; the rest are
  its highest-value works in other categories, the same ~35 rows in every
  dossier (the corpus generator pads the table to 48). Only the rows in the
  tender's category are past performance for that tender, so only they become
  CITES edges (row number kept on the edge). Linking the padding too made every
  large work "cited by all 6 past bids".
* Annexure E reproduces the full project-manager roster (all 39 people on
  file, including staff who joined after the submission date). It names no
  proposed team, so no PROPOSES edge is created; the Bid records how many
  people the annexure lists. PROPOSES is created only for an annexure that
  names a subset of the roster.
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
        if section == "D" and not re.match(r"\s*d\.\d+ ", pg, re.I):   # the table, not the d.N detail pages
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
            "rows_pkgs": list(dict.fromkeys(cited)), "people": sorted(set(people)), "security": security}


def load_bids(g, pages) -> list[dict]:
    bids = [parse_dossier(d, t) for d, t in sorted(pages.raw.items()) if d.startswith("DOC-DOSSIER-")]
    roster = {r["k"] for r in g.read("MATCH (p:Person) RETURN p.key AS k")}
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
        # Annexure D: only the rows in the tender's own category (see the module docstring)
        g.run("""MATCH (b:Bid {key:$ref}) UNWIND range(0, size($pkgs) - 1) AS i
                 MATCH (w:Work) WHERE w.title ENDS WITH ('Pkg-' + toString($pkgs[i])) AND w.category = b.category
                 CREATE (b)-[:CITES {annexure:'D', row:i + 1}]->(w)""", {"ref": b["tender_ref"], "pkgs": b["rows_pkgs"]})
        cited = g.scalar("MATCH (:Bid {key:$ref})-[c:CITES]->() RETURN count(c)", {"ref": b["tender_ref"]})
        # Annexure E: a full-roster listing names nobody in particular
        named = [norm(p) for p in b["people"]]
        full_roster = len(set(named) & roster) >= len(roster)
        if not full_roster:
            g.run("""MATCH (b:Bid {key:$ref}) UNWIND $ppl AS nm
                     MATCH (p:Person {key:nm}) CREATE (b)-[:PROPOSES {annexure:'E'}]->(p)""",
                  {"ref": b["tender_ref"], "ppl": named})
        g.run("""MATCH (b:Bid {key:$ref})
                 SET b.annexure_d_rows = $rows, b.cited_in_category = $cited,
                     b.annexure_e_listed = $listed, b.annexure_e_full_roster = $full""",
              {"ref": b["tender_ref"], "rows": len(b["rows_pkgs"]), "cited": cited, "listed": len(named),
               "full": full_roster})
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

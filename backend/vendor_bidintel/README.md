# vendor_bidintel — reused ingestion layer (credited)

Verbatim copy of `src/` from **bits-hackathon-bid-intelligence**
(github.com/shoyammishra/bits-hackathon-bid-intelligence, by Shoyam Mishra and
Atharv Gaur, BITS Hackathon Aug 2026). It is BidBrain's *ingestion layer*: the
deterministic PDF/xlsx extractors that turn the 687-document corpus into
`results/raw/facts.json`.

Nothing in here touches FalkorDB. All graph modelling, Cypher, agents, the
conflict engine and the evaluation are new work under `backend/bidbrain/` and
`backend/eval/`.

- `answer.py` / `domains.py` (the old regex question engine) are kept **only as
  the evaluation oracle** — BidBrain never calls them at runtime.
- `dataset/` is the MIT-licensed synthetic corpus
  (github.com/satvikGIKA/BITS-Hackathon-Dataset); see `DATASET_LICENSE`.
  Locally a symlink; in Docker it is cloned at a pinned commit.
- `results/raw/text_cache.json` is the layout-preserving text of every PDF,
  pages separated by `\f` — used for page-level provenance and chunk embedding.

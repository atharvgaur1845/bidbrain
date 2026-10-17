---
title: BidBrain
emoji: 🧠
colorFrom: yellow
colorTo: gray
sdk: docker
app_port: 7860
pinned: true
license: mit
short_description: FalkorDB company brain — agents prove absences & ripple
---

# BidBrain: the bid desk's company brain, on FalkorDB

Agents qualify government tenders clause by clause, prove absences, settle conflicting documents as they arrive,
and show the path to the PDF page behind every answer. Every visitor works in their own copy of the graph
(`GRAPH.COPY`).

* **Code, data model, Cypher catalog, evaluation:** https://github.com/atharvgaur1845/bidbrain
* **MCP endpoint:** `https://<this-space>.hf.space/mcp/` (streamable HTTP, read-only graph tools)

Built for Graph Hacks (WeMakeDevs × FalkorDB).

**Operators:** behind the Spaces proxy the API needs `FORWARDED_HOPS=1` to key rate limits on the real client
address; `start.sh` sets it automatically when `SPACE_ID` is present (outside Spaces the default is 0, trust only
the socket peer). `ORG_ADMIN_TOKEN` (a Space secret) lets an operator approve publications and retire company
rules or facts.

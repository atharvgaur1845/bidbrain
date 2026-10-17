"""Importing this package registers every tool in `registry.TOOLS`."""
from bidbrain.tools import algos, case, compliance, lineage, proof, read, search, trace, impact  # noqa: F401
from bidbrain.tools.registry import TOOLS, call, compact, schemas  # noqa: F401

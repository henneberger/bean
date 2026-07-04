"""Built-in connectors — one module per source, each exposing the sync-pipeline contract
(`sync`, `connect`, `connected`). The registry in `bean/sources.py` wires them into
`CORE_SOURCES`; drop-in plugins under ~/.bean/plugins/ add more the same way."""

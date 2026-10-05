# embedding-index: a complete search plugin

A vector index that runs **outside** the hub. The hub keeps it in step with what an instance is allowed to see and asks
it for candidate record ids when you call `memory_focus`; see
[docs/PLUGINS.md § Search plugins](../../../docs/PLUGINS.md#search-plugins-an-optional-embedding-index).

```bash
export INDEX_PLUGIN_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
uvicorn app:app --port 9100                       # EMBEDDER=hash by default: offline, lexical-ish, no downloads

# a real model instead (written against the documented API shapes; not run against real servers here):
EMBEDDER=ollama OLLAMA_URL=http://127.0.0.1:11434 EMBED_MODEL=nomic-embed-text uvicorn app:app --port 9100
EMBEDDER=openai EMBED_URL=https://your-endpoint EMBED_MODEL=text-embedding-3-small EMBED_API_KEY=... uvicorn app:app --port 9100

# on the hub:
export MEMORY_HUB_REMOTE_PLUGINS='[{"key":"embeddings","url":"http://127.0.0.1:9100","secret_env":"INDEX_PLUGIN_SECRET"}]'
```

Add an instance in **Settings → Plugins**, choose which scopes and projects it may index, and decide the egress level:
`metadata` sends names, descriptions and topics; `full` also sends bodies. Press **Rebuild index** to send everything
now; after that the hub sends only changes, and removes anything that is edited out of the allowlist, archived or
deleted. The default `hash` embedder is lexical (it matches shared words and word pairs, not meaning) — use a model
backend for genuinely semantic matches.

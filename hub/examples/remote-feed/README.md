# remote-feed: a complete remote plugin

An RSS/Atom feed reader that runs **outside** the hub and brings new entries into its inbox. It is both a usable plugin
and the reference for writing your own in any language: the hub talks to it over the signed JSON protocol in
[docs/PLUGINS.md](../../../docs/PLUGINS.md#remote-out-of-process-plugins).

```bash
pip install uvicorn            # and acm-core-memory-hub, or copy acm_hub/plugins/remote_sdk.py next to app.py
export FEED_PLUGIN_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
uvicorn app:app --port 9000

# on the hub (same secret value, under the NAME you register):
export FEED_PLUGIN_SECRET=...   # the hub reads the key from this variable
export MEMORY_HUB_REMOTE_PLUGINS='[{"key":"feed","url":"http://127.0.0.1:9000","secret_env":"FEED_PLUGIN_SECRET"}]'
```

Then add an instance in **Settings → Plugins**; the form comes from the service's manifest. Captured entries land in your
inbox marked `plugin:feed` and external, never straight into memory.

What it demonstrates: signing and verification (`remote_sdk`), a manifest with typed config fields, `validate`, `pull`
returning items for the hub to file, the `inbox_scope`/`inbox_project` fields the hub understands for sources, and
treating network input as hostile (DOCTYPE refused, size cap, https-only unless the operator opts in).

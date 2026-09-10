# ADR 0001: Pilot architecture

- **Status:** Accepted
- **Date:** 2026-09-09

## Context

The pilot serves one photographer and one event under a ten-day delivery window and INR 5,000
monthly infrastructure budget. One Python-focused engineer must operate it. Bulk processing needs
to survive unreliable connectivity, while customer media and face data must remain private.

## Decision

Use one Python monorepo containing a PySide6 desktop client, a Django server, and shared contract,
storage, and vision packages. Run bulk derivative and face processing on the photographer's CPU.
Keep relational data and event-scoped vectors in PostgreSQL/pgvector, private image objects in
Cloudflare R2, and the Django service on Railway. Use server-rendered pages with small HTMX/Alpine
enhancements.

## Consequences

API and model contract changes can be updated atomically across desktop and server. The desktop
client needs durable SQLite state and carefully versioned model artifacts. The server remains the
authorization control plane and never gives browsers storage-parent credentials. A later cloud
worker can implement the same processing contracts without splitting the pilot into services now.

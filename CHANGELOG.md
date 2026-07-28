# Changelog

All notable changes to `vbwd-plugin-office` are documented here.
This project follows the platform's date-based version scheme.

## [0.1.0] — 2026-07-28

First release. The VBWD Office bundle's backend, delivered as sprint S147 (slices S147-00
through S147-4).

### Added

**Phase 0 — the plugin**
- `OfficePlugin(BasePlugin)`, plugin id `office`, no hard dependencies.
- `GET /api/v1/office/meta` — a capability probe that proves the plugin is *enabled*, not merely
  mounted. It reports a real document count and degrades to `0` rather than raising: a `/meta`
  that 500s emits exactly the signal that means "disabled plugin", which would destroy the
  probe's purpose.
- Claims its own filespace at `var/office/` via `filesystem_manager.for_plugin("office")`.
- Additive RBAC: `office.use` (access level) and `office.documents.{view,manage}` (admin).

**VBWD Space (S147-1)**
- `office_node` / `office_document` / `office_version` / `office_usage`; append-only versioning.
- `DocumentStore` — the sole reader/writer of the filespace, with per-document envelope
  encryption over raw bytes (binary-safe).
- Upload size cap, per-user quota checked before *and* after write, server-side MIME sniffing
  (the client's `Content-Type` is recorded, never trusted), sha256 written and verified on read.
- Folders, rename, move, trash/purge, version restore.

**Sharing (S147-2)**
- `office_share` + `office_share_access` audit trail.
- `AccessResolver` — the single ACL truth source for every route, owner-side and public alike.
- Public `/api/v1/office/public/<token>*` routes, each declared via `declare_public_routes()`.
- Password-protected shares exchange a password for a scoped grant, never a session.
- Anonymous edits are attributed to the share (not to a user) and charge the owner's quota.

**VBWD Docs (S147-3)**
- Structured JSON document model — never stored HTML.
- Single-writer edit lease with heartbeat renewal and take-over; autosave rejects a stale
  `base_version_no` with `409`.
- AI helper on the core LLM Connection Manager, addressed by connection **slug**. Clients send a
  capability id and a range, never a raw prompt; per-user budget returns `429` with no provider
  call; per-document `ai_enabled` defaults off; suggestions are proposed patches the user accepts.

**VBWD Spreadsheets (S147-4)**
- Pure formula engine: lexer, precedence-climbing parser, AST interpreter, dependency graph with
  cycle detection, 41 functions across 6 families. No `eval`; purity enforced by an oracle test.
- Sparse workbook persistence, dirty-subgraph recalculation, a configurable row/column ceiling
  returning `413`, and CSV + XLSX import/export with an unmapped-formula report.
- Reuses the Docs edit lease and the same tables — **no migration was required**.

**Admin**
- `GET /api/v1/admin/office/documents` and `/storage` — metadata only, never content or tokens.

### Security

- Non-previewable content is re-advertised as `application/octet-stream` and forced to
  `attachment` with `nosniff`, so a public link can never serve active HTML from the host origin.
- Revoked, expired and non-existent share tokens are mutually indistinguishable (`404`).

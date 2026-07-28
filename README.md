# vbwd-plugin-office

**VBWD Office** — the backend of a self-hosted, privacy-first document suite for the
[VBWD platform](https://github.com/VBWD-platform): a substitute for the Google Docs /
Microsoft 365 web suite that runs on your own infrastructure.

Checkout path: `vbwd-backend/plugins/office/` · plugin id: `office`

## What it is

Three products over **one storage spine** — a document is an `office_node`, and its `doc_type`
decides which product opens it:

| Product | `doc_type` | What it does |
|---|---|---|
| **VBWD Space** | `file` | The vault: folders, upload/download, append-only versions, per-user quota |
| **VBWD Docs** | `text` | Rich-text editor over a structured JSON model, with an AI helper |
| **VBWD Spreadsheets** | `sheet` | A spreadsheet with a pure, deterministic formula engine |

Because they share one spine, they also share one tree, one trash, one version history, one
quota and — the part that matters — **one access-control path**. A Doc and a Sheet are files in
Space the moment they exist.

## Security posture, stated plainly

Documents are **encrypted at rest** (a per-document data key sealed with the app secret) and
every access is authorised server-side. It is **not** end-to-end encrypted: the server can read
document contents, because the AI helper, preview and search require it. Anyone claiming E2EE
while holding the keys would be misleading you — so we say which one this is.

Sharing is a **capability, not an identity**. A share row carries an opaque token (stored
hashed), a permission (`view` / `comment` / `edit`), an optional password and expiry, and an
`allow_anonymous` flag. The bearer gets exactly that permission on exactly that document —
never a session. Revocation is immediate.

Notable controls:

- Content that is not on the configured preview allow-list is served as
  `application/octet-stream` + `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`.
  An uploaded `.html` behind a public link must never become stored XSS on the host's own origin.
- An invalid, revoked or expired token returns a `404` **indistinguishable** from a token that
  never existed.
- A node belonging to another user returns `404`, not `403` — existence is not disclosed.
- Every public route is declared through `BasePlugin.declare_public_routes()`, so the platform's
  route-exposure oracle can hold them to an explicit, justified allow-list.

## The formula engine is pure

`office/sheet/` imports nothing from `vbwd`, Flask or SQLAlchemy, and contains **no `eval`** —
formulas are lexed, parsed to an AST and evaluated by an interpreter. A spreadsheet is
user-supplied code running on the server; `eval` there is remote code execution with extra steps.
A purity oracle test enforces the boundary. Errors (`#DIV/0!`, `#REF!`, `#NAME?`, `#CYCLE!`) are
**values in the lattice, not exceptions**, and an unmapped imported function keeps its source
text plus a `#NAME?` — silently dropping a formula yields a workbook that looks fine and is wrong.

## Install

Add one row to the SDK's `PLUGIN_REGISTRY` in `recipes/dev-install-ce.sh`:

```
"office|office|office|office-admin|"
```

`PluginMetadata.dependencies` is deliberately empty: the bundle stands alone. `subscription` is
an optional runtime peer for quota tiers, resolved defensively through the core entitlement seam,
so this plugin's migration root and CI clone-set never drag the subscription chain along.

## Development

```bash
# from vbwd-backend/
./bin/pre-commit-check.sh --plugin office --full
```

Run Black through the container (`docker compose exec -T api black plugins/office/`) — the host
and container versions can disagree, and the gate uses the container's.

## Companion repos

- [`vbwd-fe-user-plugin-office`](https://github.com/VBWD-platform/vbwd-fe-user-plugin-office) — Space, Docs, Sheets, public share page
- [`vbwd-fe-admin-plugin-office`](https://github.com/VBWD-platform/vbwd-fe-admin-plugin-office) — admin documents / storage / shares

## Licence

BSL 1.1 with a Bitcoin-denominated Additional Use Grant — free for commercial use while annual
VBWD-attributable sales stay below the value of 6.7 BTC. Change Licence: Apache-2.0. See
[`LICENSE`](./LICENSE).

# DB Schema — Design Decision

## Fully Normalised 3NF with Typed Enums and Explicit Junction Tables
Every entity is its own table at third normal form. Product/SKU split: products hold shared attributes, skus hold variant dimensions (size, colour) as typed columns with independent stock. Order lifecycle uses a PostgreSQL ENUM type for status plus an order_status_history table for the timeline. Money stored as NUMERIC(12,2) with a separate currency code column. Promo codes in a dedicated promos table with discount_type ENUM ('pct','flat'). Returns as a first-class return_requests table linked to order_items. Cart as a persisted carts + cart_items table with a reservation_expires_at TIMESTAMPTZ. Guest checkout via nullable user_id on orders with a guest_email column. RBAC via roles, permissions, and role_permissions junction tables. Notifications in a standalone notifications table polled by REST. Stock reservation via a reserved_qty column on skus updated in a transaction. Payment attempts tracked in a payment_attempts table (provider-agnostic, status ENUM). Taxes stored as gst_rate and gst_amount numeric columns on order_items.

**Pros**
- Referential integrity enforced everywhere — orphaned order items, dangling SKU refs, and double-counted stock are structurally impossible
- Order status timeline (REQ-11, REQ-14) is a natural query on order_status_history with timestamps; no JSON parsing
- Stock reservation and release (REQ-3, REQ-15, REQ-17) are clean atomic UPDATE + SELECT FOR UPDATE on skus.reserved_qty with no ambiguity
- Promo codes with percentage/flat types (REQ-49, REQ-52) map cleanly to ENUM + two nullable columns; query-time validation is trivial
- RBAC (REQ-48, REQ-51) expressed relationally makes permission checks straightforward JOINs auditable by non-engineers
- GST amounts per line item stored explicitly (BR-5, BR-7) make tax reporting and refund calculations exact without recomputation
- Return flow two-step lifecycle (REQ-6) modelled as return_requests.status ENUM ('requested','approved','rejected') is immediately auditable
- Reporting (REQ-50) across orders, promos, returns is efficient because aggregations run on well-indexed numeric columns, not parsed blobs

**Cons**
- Catalogue browsing queries (REQ-38, REQ-40, REQ-45, REQ-46) with multi-filter + facet counts require multi-join queries; may need materialised views or partial indexes as catalogue grows
- Product variant attributes (size, colour) are hard-coded columns — adding a new dimension (e.g. material) requires a schema migration
- Autocomplete search (REQ-41, REQ-44) needs pg_trgm or tsvector indexes; adequate at small scale but the team has already allocated Elasticsearch for this, creating a dual-write concern
- More tables means slightly more boilerplate ORM mapping in the Node/Express layer

## Hybrid: Normalised Transactional Core with JSONB for Catalogue and Notification Payloads
Transactional entities (orders, order_items, skus, carts, payment_attempts, return_requests, promos, users, roles) remain fully normalised with strict types. Product catalogue gets a JSONB attributes column on both products and skus to store arbitrary variant dimensions and CMS-managed fields without schema migrations. Order status timeline stored as a JSONB array column on orders (append-only event log) rather than a separate history table, enabling single-row order reads for the detail page. Notification payloads stored as JSONB in a notifications table. Addresses stored as JSONB on orders (snapshot at time of purchase) and as a normalised addresses table for the account address book. PIN serviceability stored as a serviceable_pincodes table. Tax breakdown stored as a JSONB taxes object on order_items alongside numeric totals. Money as NUMERIC(12,2). Elasticsearch fed via a lightweight sync worker reading from the products/skus tables.

**Pros**
- Catalogue CMS (REQ-53, REQ-55) can add new product attributes (ingredients, certifications, fit-guide) without DDL changes — merchandisers iterate freely
- Order detail page (REQ-5, REQ-11) fetches a single orders row including the embedded timeline; fewer JOINs under read load
- Variant dimensions in JSONB (size, colour, material) support BR-3 without locking the schema to exactly two dimensions
- Notification payload flexibility (REQ-56) — different notification types can carry different fields without a union-table anti-pattern
- Address snapshot in JSONB on orders is the correct model for immutable historical record of where an order was shipped (avoids stale address bug if user later edits their address book)
- Transactional core (stock, payments, refunds, RBAC) stays fully typed and integrity-constrained where it matters most
- GIN index on JSONB attributes supports catalogue filtering queries used before Elasticsearch is warm

**Cons**
- JSONB status timeline on orders means querying 'all orders currently in Packed status' requires jsonb path operators or a redundant current_status column — a denormalisation smell
- JSONB timeline loses the ability to enforce that each status transition is recorded by a specific staff member with a FK to users without extra application-layer discipline
- Reporting (REQ-50) that aggregates over embedded JSONB fields (e.g. tax amounts by category) is slower and less readable than plain column aggregations
- Two representations of the same data (JSONB attributes + Elasticsearch index) require a reliable sync strategy; out-of-sync states create inconsistent filter counts (REQ-40)
- Team must maintain discipline about which fields belong in JSONB vs typed columns — without a convention, the schema drifts over time

## Event-Sourced Order Aggregate with Denormalised Read Models
Order lifecycle is stored as an append-only order_events table (event_type, payload JSONB, occurred_at, actor_id) — the authoritative source of truth for all order state transitions, cancellations, returns, refunds, and payment outcomes. A separate order_snapshots table (or materialised view) is maintained by a projection function and holds the current denormalised order state for fast reads. Cart, SKU stock, and payment_attempts remain as mutable relational tables because they are operational, not historical. Products, SKUs, users, roles, and promos are fully normalised conventional tables. The notifications table is an event projection itself. Stock reservation is a row in stock_reservations with expiry timestamp rather than a delta on skus.reserved_qty. Refunds are events of type 'refund_issued' in order_events, satisfying REQ-13 without a separate refunds table.

**Pros**
- Complete, tamper-evident audit trail of every order state change (REQ-4, REQ-11, REQ-14) is a first-class citizen, not reconstructed from history table
- Returns two-step flow (REQ-6, REQ-7) and cancellation (REQ-2, REQ-9) are additional event types — no schema change needed to add new lifecycle steps
- Refund recorded against originating order (REQ-13) is structurally guaranteed — refund_issued events are children of the order aggregate by design
- Time-travel queries (what was the order state at T?) are trivially answered by replaying events up to T — useful for dispute resolution
- Order detail page timeline (REQ-11) is a direct SELECT from order_events filtered by order_id, no reconstruction needed

**Cons**
- Significant complexity overhead for a team building an MVP e-commerce platform — event sourcing is only justified when auditability or temporal queries are primary requirements, not secondary ones
- Current order status for staff order management (REQ-4, REQ-10) requires either querying the snapshot table (eventual consistency risk) or replaying events on every request (expensive)
- Stock reservation consistency (REQ-3, REQ-15, REQ-17) becomes harder — stock_reservations as a separate table must be kept in sync with order events, introducing a two-phase commit or saga pattern that the Node/Express stack is not designed for
- Reporting (REQ-50) across aggregate state requires the snapshot/materialised view to always be current; operational burden is high for a small team
- RBAC enforcement (REQ-48, REQ-51) on event streams is non-trivial — which events can which roles read? Standard row-level security does not map cleanly to event payloads
- Guest checkout (REQ-24) and cart persistence (REQ-32) do not fit the event-sourced model naturally and end up as conventional tables anyway, creating a mixed paradigm
- Over-engineered relative to the stated constraints — the build must be testable without live keys and must move fast; event sourcing slows initial velocity significantly

## Chosen: Hybrid: Normalised Transactional Core with JSONB for Catalogue and Notification Payloads
This app has two distinctly different data characters that this approach handles cleanly. The transactional core — orders, order_items, skus, stock reservations, payment_attempts, return_requests, promos, carts, users, roles — has hard integrity requirements (BR-3, BR-4, stock reservation atomicity in REQ-3/REQ-15/REQ-17, refund linkage in REQ-13, two-step return lifecycle in REQ-6) that demand strict typing, foreign keys, and NUMERIC money. Keeping this normalised prevents the class of bugs that matter most in an e-commerce system. The catalogue and CMS layer, by contrast, needs to evolve without engineering tickets (REQ-53, BR-3 implies variants may grow beyond size/colour), which is exactly JSONB's strength. The address-as-snapshot pattern on orders is also the correct model for historical accuracy. The one genuine weakness — querying current_status from an embedded timeline — is resolved by retaining a current_status ENUM column on orders as a denormalised fast-path, making the JSONB timeline purely an append-only audit log rather than the query target. This avoids the key cons of the pure JSONB approach while preserving schema flexibility where it is genuinely needed. Fully Normalised 3NF was close but its rigid variant column schema would create friction for the catalogue CMS requirement and merchandiser workflows. Event Sourcing is inappropriate at this scale and team size — its audit benefits are achievable with the order_status_history table of approach 1 at a fraction of the operational complexity.
# Backend Structure — Design Decision

## Classic Layered (MVC-style)
Organise the entire codebase by technical role: routes/, controllers/, services/, repositories/, models/, middleware/, config/. Every feature (orders, cart, auth, catalogue, etc.) is represented by one file in each layer. Cross-cutting concerns live in dedicated top-level folders (middleware/, utils/, config/). The ORM/query layer (Sequelize or Knex) models map 1-to-1 to the 22 persisted tables.

**Pros**
- Immediately familiar to any Express developer; zero onboarding friction for new hires.
- Enforces consistent call direction (route → controller → service → repository) across all 22 entities.
- Centralised middleware/ folder makes it trivial to apply rate-limiting, RBAC guards, and JWT verification globally or per-router.
- Simple to reason about shared dependencies: the promotions engine or stock-reservation logic can be a service called from both the cart service and the checkout service without circular-module concerns.

**Cons**
- With 7 major feature areas and 22 tables, each layer folder will hold 15-20 files; navigating between the order controller and the order service requires jumping across the full tree.
- Domain coupling is invisible: nothing in the structure stops the cart controller from importing the order repository directly, eroding layer discipline over time.
- The Elasticsearch integration (catalogue search/autocomplete) and the payment abstraction layer (provider-agnostic interface) have no natural home and tend to get stuffed awkwardly into services/ or utils/.
- Admin routes mirror almost every customer route (products, orders, users, promos, returns, reports); without sub-namespacing those files pile up in a single routes/ folder.

## Domain-Modular (Feature Slices)
Group code by business domain, not technical role. Top-level src/modules/ contains one folder per bounded context: auth/, catalogue/, cart/, checkout/, orders/, payments/, promotions/, returns/, notifications/, admin/, search/. Each module owns its own router, controller, service, repository, and any module-specific middleware or DTOs. Shared infrastructure (db connection, JWT helpers, error classes, generic middleware, config) lives in src/core/ or src/shared/. Elasticsearch indexing lives inside search/; the payment abstraction lives inside payments/ as an interface + mock adapter.

**Pros**
- Each of the 7 feature areas maps to one or two modules; a developer working on returns touches only src/modules/returns/ and its edges into orders/.
- The payment provider-agnostic constraint is naturally expressed as payments/adapters/mock.adapter.js + payments/adapters/stripe.adapter.js behind a payments/payment.interface.js — swapped via config without touching other modules.
- Elasticsearch search module is self-contained: it owns the index-sync logic (catalogue events → ES) and the query/autocomplete endpoints, keeping MySQL catalogue writes separate from ES reads.
- Admin module can re-use domain services (e.g. orders.service, catalogue.service) by importing them, making the admin a thin orchestration layer rather than duplicating business logic.
- Scales well: adding a Reviews module post-launch means creating one new folder with zero surgery to existing modules.
- Aligns with the 40+ frontend routes which cluster tightly by domain, making API versioning and route grouping self-documenting.

**Cons**
- Higher initial setup cost: 11 modules each need their own router registration and wiring into the Express app.
- Risk of circular imports if modules are not disciplined (e.g. cart importing orders and orders importing cart for stock reservation). Requires a clear dependency graph rule: cart may call orders; orders must not call cart.
- Shared logic (e.g. serviceable pin-code validation used by both checkout and address management) needs an explicit home in shared/ or a dedicated addresses module — easy to get wrong early.
- Junior developers unfamiliar with modular patterns may still reach across module boundaries; needs a documented architecture decision record.

## Layered Core + Domain Plug-ins (Hybrid)
Retain a thin global technical scaffold (app.js, src/config/, src/core/db, src/core/auth, src/core/middleware/, src/core/errors/) but inside src/api/ organise by domain feature rather than technical role. Each domain folder (api/catalogue/, api/orders/, api/cart/, api/checkout/, api/payments/, api/auth/, api/admin/, api/notifications/, api/search/) contains routes.js, controller.js, service.js, and repository.js co-located. The repository layer is the only place Knex/Sequelize queries live; services are pure business logic; controllers are thin HTTP adapters. A shared src/core/repository-base.js provides common CRUD helpers all repositories extend.

**Pros**
- Balances discoverability (all order-related files are in api/orders/) with strict layering enforced by the base-repository pattern.
- The global core/ layer cleanly houses JWT strategy, bcrypt helpers, rate-limiter config, error middleware, and DB pool — cross-cutting concerns that truly belong globally rather than being duplicated per module.
- Repository-base.js gives consistent query patterns across all 22 tables while still allowing domain-specific query methods per repository.
- Easier to enforce no-cross-domain repository imports via lint rules (each service may only import its own repository and services from core or an allowed dependency list).
- Admin and customer API share the same domain services but mount separate Express routers with different RBAC middleware, avoiding route-file sprawl.

**Cons**
- Two mental models coexist (technical layering at the core, domain slices at the api level); newcomers must understand both.
- The repository-base abstraction can become a leaky pseudo-ORM if not kept minimal, causing the same over-generalisation problems as full ORMs.
- With 9 domain folders each having 4 files, the api/ directory still has 36 files; not as clean as pure modules with sub-folders.
- Less idiomatic for the Node/Express ecosystem than either pure layered or pure modular; fewer open-source examples to reference.

## Chosen: Domain-Modular (Feature Slices)
This app has 7 distinct, relatively independent business capabilities (catalogue, cart, checkout, orders, payments, returns, notifications) each with their own data objects, business rules, and admin mirrors. The domain-modular approach maps directly onto that reality: each module is a self-contained vertical slice that a developer can own end-to-end, which reduces cognitive overhead and merge conflicts on a team building all features in parallel. The two hardest architectural constraints — the provider-agnostic payment abstraction and the Elasticsearch search isolation — are both expressed cleanly as self-contained modules with internal adapter patterns, rather than being awkward guests in a flat services/ folder. The admin surface, which touches almost every domain, becomes a thin orchestration module that imports domain services rather than duplicating logic. With 40+ frontend routes clustering tightly by domain, the module boundaries will stay stable over the product's lifetime. The main risk (circular imports) is manageable with a one-page dependency-direction rule (auth ← everything; cart → orders/promotions; checkout → cart/orders/payments; admin → all domain services) documented as an ADR and enforced with an ESLint import boundary plugin.

## Resulting layout
```
src/
  modules/
    auth/
      auth.routes.js  — Express router: POST /auth/register, /auth/login, /auth/logout, /auth/forgot-password, /auth/reset-password
      auth.controller.js  — Request handling for all auth endpoints
      auth.service.js  — Registration, login, JWT issuance, bcrypt hashing, password-reset token lifecycle
      auth.validator.js  — Joi/express-validator schemas for auth payloads
      auth.test.js  — Unit + integration tests for auth flows
    users/
      users.routes.js  — Express router: GET/PATCH /users/me, admin CRUD /admin/users
      users.controller.js  — Request handling for profile, address-book, admin user management
      users.service.js  — Profile updates, role assignment, account lookup
      users.validator.js  — Validation schemas for user payloads
      users.test.js  — Unit tests for user service
    roles/
      roles.routes.js  — Express router: admin role management endpoints
      roles.controller.js  — Request handling for roles and user_roles assignment
      roles.service.js  — RBAC role CRUD, user-role association logic
      roles.test.js  — Unit tests for RBAC logic
    addresses/
      addresses.routes.js  — Express router: GET/POST/PUT/DELETE /users/me/addresses
      addresses.controller.js  — Request handling for address book management
      addresses.service.js  — Address CRUD, default address logic, serviceability check against serviceable_pin_codes
      addresses.validator.js  — Validation schemas for address payloads
      addresses.test.js  — Unit tests for address service
    catalogue/
      catalogue.routes.js  — Express router: product listing, PLP filters, product detail, categories, brands; admin sub-routes
      catalogue.controller.js  — Request handling for browse, category, brand, and admin catalogue endpoints
      catalogue.service.js  — Product/SKU/category/brand business logic, stock lookup, image handling, admin CRUD orchestration
      catalogue.validator.js  — Validation schemas for product, SKU, category, brand payloads
      catalogue.test.js  — Unit tests for catalogue service
    search/
      search.routes.js  — Express router: GET /search (full-text + facets), GET /search/autocomplete
      search.controller.js  — Request handling for search and autocomplete queries
      search.service.js  — Orchestrates Elasticsearch queries; faceted filter aggregations, autocomplete suggestions
      search.validator.js  — Validation for query params (q, filters, page, size)
      adapters/
        elasticsearch.adapter.js  — Elasticsearch client wrapper; index mapping helpers, query builders
      search.test.js  — Unit tests with mocked Elasticsearch adapter
    cart/
      cart.routes.js  — Express router: GET/POST/PATCH/DELETE /cart and /cart/items/:id; apply promo
      cart.controller.js  — Request handling for cart operations
      cart.service.js  — Add/update/remove items, stock reservation validation, promo code application, guest vs. auth cart merge
      cart.validator.js  — Validation schemas for cart item payloads
      cart.test.js  — Unit tests for cart logic
    promotions/
      promotions.routes.js  — Express router: promo validation endpoint; admin CRUD /admin/promo-codes
      promotions.controller.js  — Request handling for promo code validation and admin management
      promotions.service.js  — Promo eligibility rules, discount calculation, usage tracking
      promotions.validator.js  — Validation schemas for promo code payloads
      promotions.test.js  — Unit tests for promotions engine
    checkout/
      checkout.routes.js  — Express router: POST /checkout/initiate, /checkout/confirm; guest checkout support
      checkout.controller.js  — Request handling for multi-step checkout flow
      checkout.service.js  — Address validation, stock reservation confirmation, promo finalisation, order creation, payment intent delegation
      checkout.validator.js  — Validation schemas for checkout payloads
      checkout.test.js  — Integration tests for checkout flow
    payments/
      payments.routes.js  — Express router: POST /payments/initiate, /payments/confirm, /payments/webhook
      payments.controller.js  — Request handling for payment lifecycle
      payments.service.js  — Provider-agnostic payment orchestration; persists payment_attempts, delegates to active adapter
      payments.validator.js  — Validation for payment request payloads
      adapters/
        payment.adapter.interface.js  — Abstract interface / duck-type contract all provider adapters must satisfy
        mock.adapter.js  — Test-mode mock adapter returning configurable success/failure responses
      payments.test.js  — Unit tests with mock adapter
    orders/
      orders.routes.js  — Express router: GET /orders, GET /orders/:id; admin order list and detail; status update
      orders.controller.js  — Request handling for order history, detail, admin management
      orders.service.js  — Order creation (called by checkout), status transitions, order_status_history writes, order_tracking updates
      orders.validator.js  — Validation for order update payloads
      orders.test.js  — Unit tests for order service
    returns/
      returns.routes.js  — Express router: POST /orders/:id/returns; GET /orders/:id/returns/:rid; admin returns list and detail
      returns.controller.js  — Request handling for return initiation and admin management
      returns.service.js  — Return eligibility, return_requests CRUD, refund trigger, stock update on approval
      returns.validator.js  — Validation schemas for return request payloads
      returns.test.js  — Unit tests for returns service
    notifications/
      notifications.routes.js  — Express router: GET /notifications (polling), PATCH /notifications/:id/read, PATCH /notifications/read-all
      notifications.controller.js  — Request handling for notification centre
      notifications.service.js  — Notification creation (called by other services), unread count, mark-read logic
      notifications.test.js  — Unit tests for notification service
    admin/
      admin.routes.js  — Express router: mounts admin sub-routes; applies admin RBAC middleware
      admin.controller.js  — Dashboard stats aggregation, reports endpoint orchestration
      admin.service.js  — Cross-domain read aggregations for dashboard and reports (delegates to domain services)
      admin.test.js  — Integration tests for admin endpoints
  db/
    client.js  — Knex instance configured from env; exported singleton used by all repositories
    migrations/
      001_create_roles.js  — roles table
      002_create_users.js  — users table with bcrypt password_hash column
      003_create_user_roles.js  — user_roles join table
      004_create_addresses.js  — addresses table FK → users
      005_create_serviceable_pin_codes.js  — serviceable_pin_codes lookup table
      006_create_categories.js  — categories table with self-referencing parent_id
      007_create_brands.js  — brands table
      008_create_products.js  — products table FK → categories, brands
      009_create_product_images.js  — product_images table FK → products
      010_create_skus.js  — skus table FK → products with size/colour/stock columns
      011_create_promo_codes.js  — promo_codes table with rules JSON column
      012_create_carts.js  — carts table (nullable user_id for guest, session_id)
      013_create_cart_items.js  — cart_items table FK → carts, skus
      014_create_orders.js  — orders table FK → users (nullable), addresses
      015_create_order_items.js  — order_items table FK → orders, skus
      016_create_order_status_history.js  — order_status_history table FK → orders
      017_create_stock_reservations.js  — stock_reservations table FK → skus, orders
      018_create_payment_attempts.js  — payment_attempts table FK → orders
      019_create_refunds.js  — refunds table FK → orders, payment_attempts
      020_create_return_requests.js  — return_requests table FK → orders
      021_create_order_tracking.js  — order_tracking table FK → orders
      022_create_notifications.js  — notifications table FK → users (nullable for broadcast)
    seeds/
      01_roles.js  — Seed default roles: customer, staff, admin
      02_admin_user.js  — Seed default admin user for development
      03_categories.js  — Sample category tree
      04_brands.js  — Sample brands
      05_products_skus.js  — Sample products and SKU variants
      06_promo_codes.js  — Sample promo codes
    repositories/
      users.repository.js  — Knex queries for users table
      roles.repository.js  — Knex queries for roles and user_roles tables
      addresses.repository.js  — Knex queries for addresses table
      serviceable_pin_codes.repository.js  — Knex queries for serviceability lookup
      categories.repository.js  — Knex queries for categories table (tree traversal helpers)
      brands.repository.js  — Knex queries for brands table
      products.repository.js  — Knex queries for products and product_images tables
      skus.repository.js  — Knex queries for skus table; atomic stock decrement helpers
      promo_codes.repository.js  — Knex queries for promo_codes table
      carts.repository.js  — Knex queries for carts and cart_items tables
      orders.repository.js  — Knex queries for orders, order_items, order_status_history, order_tracking tables
      stock_reservations.repository.js  — Knex queries for stock_reservations table
      payment_attempts.repository.js  — Knex queries for payment_attempts table
      refunds.repository.js  — Knex queries for refunds table
      return_requests.repository.js  — Knex queries for return_requests table
      notifications.repository.js  — Knex queries for notifications table
  middleware/
    authenticate.js  — JWT verification middleware; attaches req.user
    authorize.js  — RBAC middleware factory: authorize(role) checks req.user roles
    rateLimiter.js  — express-rate-limit configs for auth and password-reset routes
    errorHandler.js  — Centralised Express error handler; structured JSON error responses
    requestLogger.js  — Morgan/Winston HTTP request logging
    validate.js  — Generic validation middleware wrapper for Joi schemas
  config/
    index.js  — Reads and exports all env vars with defaults and validation (dotenv + joi)
    database.js  — Knex connection config derived from config/index.js
    elasticsearch.js  — Elasticsearch client config (host, auth) derived from config/index.js
    jwt.js  — JWT secret, access token TTL, reset token TTL constants
    rateLimit.js  — Rate-limit window and max request constants
  utils/
    logger.js  — Winston logger instance (console + file transports); satisfies NFR-11
    asyncHandler.js  — Wraps async route handlers to forward errors to Express error handler
    pagination.js  — Shared helper: parse page/limit, build offset, format paginated response
    tokenUtils.js  — Generate/verify cryptographic reset tokens (crypto.randomBytes)
  app.js  — Express app factory: registers middleware, mounts all module routers, error handler
  server.js  — Entry point: imports app, starts HTTP server, logs port
tests/
  integration/
    auth.test.js  — Full-stack auth flow tests against test DB
    cart.test.js  — Cart + promo integration tests
    checkout.test.js  — End-to-end checkout with mock payment adapter
    orders.test.js  — Order lifecycle and status transition tests
    returns.test.js  — Return request and refund flow tests
    search.test.js  — Search and autocomplete with mocked Elasticsearch
  helpers/
    dbSetup.js  — Run migrations and seeds before tests; rollback after
    authHelper.js  — Generate test JWTs for different roles
    requestHelper.js  — Supertest wrapper with common headers
knexfile.js  — Knex environment configs (development, test, production) for CLI use
.env.example  — Template of all required environment variables with documentation comments
.eslintrc.js  — ESLint config including import/no-cycle and import boundary rules
package.json  — Dependencies: express, knex, pg, bcrypt, jsonwebtoken, joi, @elastic/elasticsearch, express-rate-limit, morgan, winston, cors, helmet, dotenv; devDeps: jest, supertest, nodemon
jest.config.js  — Jest config: test environment, coverage thresholds, module aliases
.gitignore  — node_modules, .env, dist, logs
README.md  — Setup, migration commands, environment variable reference, module dependency direction ADR summary
```

## Notes
- Stack is Node.js + Express with MySQL 8 per the brief, but the datastore_product field says 'postgresql' — the schema, repositories, and Knex config are written for PostgreSQL (pg driver). Confirm which database to target and swap the Knex driver accordingly.
- Knex is chosen as the query builder / migration tool because it works with both MySQL and PostgreSQL, keeps SQL explicit, and avoids heavy ORM magic — appropriate for a team already familiar with the domain. Swap for Sequelize or Prisma if ORM features are preferred.
- The notifications table is in PostgreSQL (as specified). The REST polling pattern is implemented in the notifications module; no additional infrastructure is required.
- Elasticsearch is accessed only through src/modules/search/adapters/elasticsearch.adapter.js. All other modules are isolated from it. Index sync (product indexing on catalogue changes) should be triggered from catalogue.service.js via the search adapter.
- The payment module ships only the mock adapter. Real provider adapters (Stripe, etc.) are dropped into src/modules/payments/adapters/ and selected via a PAYMENT_ADAPTER env var — no code changes required to plug them in.
- Guest checkout is supported via a nullable user_id on the carts and orders tables and a session_id column on carts. The auth middleware is not required on checkout routes; ownership is verified by session cookie or guest token.
- Password-reset tokens are stored as hashed values in the users table (or a separate password_reset_tokens table) with an expires_at column, satisfying NFR-5. Add a migration if a separate table is preferred.
- Rate limiting (NFR-7) is applied at the router level for /auth/login and /auth/forgot-password via the rateLimiter middleware.
- The import dependency direction rule (auth ← everything; cart → orders/promotions; checkout → cart/orders/payments; admin → all domain services) should be enforced with eslint-plugin-import and documented as an ADR in README.md.
- stock_reservations are created at checkout initiation and released on payment failure or cart abandonment. A scheduled cleanup job (cron or pg_cron) for stale reservations is recommended but not included in this tree.
- serviceable_pin_codes is a simple lookup table; the addresses service queries it before confirming delivery eligibility during checkout.
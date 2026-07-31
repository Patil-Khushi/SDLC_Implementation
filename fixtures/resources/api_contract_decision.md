# API Contract — Design Decision

## Pure Resource-Oriented REST
Every concept is a noun resource with flat or shallow nesting. State changes (cancel, advance status, approve return) are expressed via PATCH with a status field or PUT on the resource. Filtering, sorting, and pagination are uniform query-string conventions across all list endpoints. Auth gates are enforced by JWT claims on every route.

**Pros**
- Perfectly cacheable GET responses for catalogue, product, SKU, and order-history endpoints — important given Elasticsearch-backed search and high browse traffic.
- Uniform mental model: every frontend route maps 1-to-1 to a resource URL, making the React+Vite client straightforward to wire up.
- Simple to document and onboard new engineers because every endpoint follows the same shape (GET list, GET one, POST create, PATCH update, DELETE remove).
- Works naturally with MySQL 8 because resource boundaries mirror table boundaries.

**Cons**
- State-machine transitions (Confirmed→Packed→Shipped→Delivered, cancel-before-shipment releasing stock, return request→approval) are awkward to express as PATCH status; clients must know valid next states and the server must validate transitions inline, leading to fat PATCH handlers with implicit side-effect logic.
- Guest checkout is a multi-step flow that doesn't fit cleanly into a single resource; forcing it into PATCH on a cart or POST on an order loses the step structure the UI requires (address→review→payment).
- Promo-code validation at checkout ('does this code apply here?') is a query on a relationship, not a resource mutation — it gets shoehorned into a GET with side effects or a weird PATCH.
- Payment retry (REQ-32) and payment webhook callback are actions, not resource state; modelling them as PATCH on payment_attempts is semantically misleading.

## Hybrid REST + Action Sub-Resources
Core CRUD resources (products, skus, orders, carts, users, addresses, promo_codes, return_requests, notifications) follow standard REST with shallow nesting. Explicit state-machine transitions and multi-step flows are modelled as action sub-resources (POST /orders/:id/cancel, POST /orders/:id/advance-status, POST /return-requests/:id/approve, POST /return-requests/:id/reject, POST /carts/:id/apply-promo, POST /checkout/sessions, POST /payment-attempts/:id/retry). Filtering uses typed query params; pagination uses cursor-based approach for order history and offset for catalogue. RBAC enforced via JWT role claims + Express middleware per router.

**Pros**
- State transitions are self-documenting and single-purpose: POST /orders/:id/cancel triggers stock release, POST /orders/:id/advance-status validates the state machine and appends to order_status_history — all side effects live in one named handler, not a bloated PATCH.
- Checkout modelled as a session resource (POST /checkout/sessions, PATCH /checkout/sessions/:id/address, PATCH /checkout/sessions/:id/promo, POST /checkout/sessions/:id/confirm) cleanly maps to the three frontend checkout steps and preserves cart on payment failure (REQ-32) without ambiguity.
- Guest checkout requires no authenticated user — the checkout session carries an optional user_id and a guest email; converting post-checkout is a separate POST /users action (REQ-24, REQ-29).
- Payment abstraction (REQ-33) maps naturally: POST /payment-attempts (initiate), POST /payment-attempts/:id/retry, POST /payments/webhook — the provider-agnostic module sits behind these three endpoints.
- Catalogue CRUD for admin (products, skus, categories, brands) stays pure REST, keeping the admin module simple and consistent with standard CMS patterns.
- Search endpoint (GET /search/suggestions?q=, GET /search/products?q=&brand=&price_min=&price_max=&rating=&sort=&page=) remains a clean GET with typed query params, easy to back with Elasticsearch.
- RBAC is cleanly layered: customer routes, staff routes (/admin/*), and merchandiser routes each get their own Express router with role-middleware guards, satisfying REQ-48 and REQ-51 without cross-cutting complexity.

**Cons**
- More endpoint surface area than pure REST; junior engineers may inconsistently choose between PATCH and a named action when adding new features.
- Checkout session is a pseudo-resource that lives in the checkout module rather than mapping directly to a DB table — requires clear documentation to avoid confusion.
- Two paradigms (REST + action sub-resources) require a documented convention to prevent drift over time.

## RPC-Style Action-First API
All endpoints are named actions organised by domain module, not resources. URLs read like procedure calls: POST /orders/cancel, POST /orders/advance, POST /returns/request, POST /returns/approve, POST /cart/add-item, POST /cart/apply-promo, POST /checkout/confirm, POST /auth/login, POST /auth/register. GETs still exist for reads but are module-namespaced rather than resource-namespaced.

**Pros**
- Every business operation has its own unambiguous URL — easy for the team to trace a frontend action to a backend handler.
- Eliminates the REST impedance mismatch for state machines and multi-step flows entirely; no PATCH/PUT confusion.
- Very fast to prototype: each Express route is a standalone function with no resource-model contract to honour.

**Cons**
- Loses HTTP semantics: GET cacheability is undermined when reads are mixed with RPC-style namespacing; Elasticsearch-powered catalogue queries need proper GET semantics for CDN/browser caching.
- No standard pagination, filtering, or sorting contract — every list endpoint risks inventing its own shape, creating inconsistent client-side data-fetching patterns across the 40+ frontend routes.
- RBAC is harder to layer cleanly: REST routers allow middleware per resource prefix (/admin/*), but RPC flat namespacing requires per-function guards, increasing surface area for misconfiguration.
- REST tooling (OpenAPI generation, API clients, Postman collections) is significantly more valuable when resources have stable noun URLs — losing that makes documentation and testing harder for a team that needs to move fast.
- Admin CRUD (catalogue management, user management, promo codes) becomes verbose and repetitive without resource conventions — every field update needs its own action endpoint.

## Chosen: Hybrid REST + Action Sub-Resources
This app has two distinct API personalities that must coexist: (1) standard CRUD for catalogue, users, addresses, promo-code management, and admin screens — where pure REST is optimal, cacheable, and directly mirrors the MySQL schema; and (2) a set of explicit business operations with side effects and state-machine semantics — order cancellation releasing stock, order status advancement, return request/approval, checkout multi-step session, payment initiation and retry, promo-code application — where named action sub-resources are far safer and clearer than overloaded PATCH handlers. The hybrid approach preserves full HTTP GET cacheability for the high-traffic catalogue and search paths (critical for the Elasticsearch integration), gives the React frontend a predictable 1:1 mapping between UI interactions and API calls, allows RBAC middleware to be mounted cleanly per router prefix, and keeps the provider-agnostic payment module behind three well-defined endpoints. The two-paradigm cost is real but manageable with a short API convention document, which is a far smaller burden than the alternatives: pure REST's bloated PATCH handlers with hidden side effects, or RPC's loss of HTTP semantics and caching.

## Endpoints (98)
- 🌐 `POST /auth/register` — Register a new customer account
- 🌐 `POST /auth/login` — Authenticate and obtain JWT
- 🔒 `POST /auth/logout` — Invalidate current session [customer, staff, merchandiser, admin]
- 🌐 `POST /auth/forgot-password` — Request a password-reset token
- 🌐 `POST /auth/reset-password` — Reset password using a token
- 🔒 `POST /auth/guest/convert` — Convert a guest account to a full account [customer]
- 🔒 `GET /users/me` — Get the authenticated user's profile [customer, staff, merchandiser, admin]
- 🔒 `PATCH /users/me` — Update the authenticated user's profile [customer, staff, merchandiser, admin]
- 🔒 `GET /users` — List all users (admin) [admin]
- 🔒 `GET /users/{userId}` — Get a user by ID (admin) [admin]
- 🔒 `PATCH /users/{userId}` — Update a user by ID (admin) [admin]
- 🔒 `DELETE /users/{userId}` — Deactivate a user account (admin) [admin]
- 🔒 `GET /roles` — List all roles [admin]
- 🔒 `POST /roles` — Create a new role [admin]
- 🔒 `GET /roles/{roleId}` — Get a role by ID [admin]
- 🔒 `PATCH /roles/{roleId}` — Update a role [admin]
- 🔒 `DELETE /roles/{roleId}` — Delete a role [admin]
- 🔒 `GET /users/{userId}/roles` — List roles assigned to a user [admin]
- 🔒 `POST /users/{userId}/roles` — Assign a role to a user [admin]
- 🔒 `DELETE /users/{userId}/roles/{roleId}` — Remove a role from a user [admin]
- 🔒 `GET /users/me/addresses` — List saved addresses for the current user [customer]
- 🔒 `POST /users/me/addresses` — Add a new saved address [customer]
- 🔒 `GET /users/me/addresses/{addressId}` — Get a saved address by ID [customer]
- 🔒 `PUT /users/me/addresses/{addressId}` — Update a saved address [customer]
- 🔒 `DELETE /users/me/addresses/{addressId}` — Delete a saved address [customer]
- 🌐 `GET /serviceability/pin-codes/{pinCode}` — Check if a PIN code is serviceable for delivery
- 🔒 `GET /admin/serviceable-pin-codes` — List all serviceable PIN codes (admin) [admin]
- 🔒 `POST /admin/serviceable-pin-codes` — Add a serviceable PIN code (admin) [admin]
- 🔒 `PATCH /admin/serviceable-pin-codes/{pinCodeId}` — Update a serviceable PIN code (admin) [admin]
- 🔒 `DELETE /admin/serviceable-pin-codes/{pinCodeId}` — Remove a serviceable PIN code (admin) [admin]
- 🌐 `GET /categories` — List all active categories
- 🔒 `POST /categories` — Create a category (admin/merchandiser) [admin, merchandiser]
- 🌐 `GET /categories/{categoryId}` — Get a category by ID
- 🔒 `PATCH /categories/{categoryId}` — Update a category (admin/merchandiser) [admin, merchandiser]
- 🔒 `DELETE /categories/{categoryId}` — Delete a category (admin) [admin]
- 🌐 `GET /brands` — List all brands
- 🔒 `POST /brands` — Create a brand (admin/merchandiser) [admin, merchandiser]
- 🌐 `GET /brands/{brandId}` — Get a brand by ID
- 🔒 `PATCH /brands/{brandId}` — Update a brand (admin/merchandiser) [admin, merchandiser]
- 🔒 `DELETE /brands/{brandId}` — Delete a brand (admin) [admin]
- 🌐 `GET /products` — List products with filters, facets, and pagination
- 🔒 `POST /products` — Create a product (admin/merchandiser) [admin, merchandiser]
- 🌐 `GET /products/{productId}` — Get a product by ID with SKUs and images
- 🔒 `PATCH /products/{productId}` — Update a product (admin/merchandiser) [admin, merchandiser]
- 🔒 `DELETE /products/{productId}` — Delete (deactivate) a product (admin) [admin]
- 🌐 `GET /products/{productId}/images` — List images for a product
- 🔒 `POST /products/{productId}/images` — Add an image to a product (admin/merchandiser) [admin, merchandiser]
- 🔒 `PATCH /products/{productId}/images/{imageId}` — Update a product image (admin/merchandiser) [admin, merchandiser]
- 🔒 `DELETE /products/{productId}/images/{imageId}` — Delete a product image (admin/merchandiser) [admin, merchandiser]
- 🌐 `GET /products/{productId}/skus` — List SKUs for a product
- 🔒 `POST /products/{productId}/skus` — Create a SKU for a product (admin/merchandiser) [admin, merchandiser]
- 🌐 `GET /products/{productId}/skus/{skuId}` — Get a specific SKU
- 🔒 `PATCH /products/{productId}/skus/{skuId}` — Update a SKU (admin/merchandiser) [admin, merchandiser]
- 🔒 `DELETE /products/{productId}/skus/{skuId}` — Delete (deactivate) a SKU (admin) [admin]
- 🌐 `GET /search` — Full-text product search with facets and pagination
- 🌐 `GET /search/autocomplete` — Autocomplete suggestions as the user types
- 🌐 `POST /carts` — Create a new cart (authenticated or guest)
- 🌐 `GET /carts/{cartId}` — Get cart with items and computed totals
- 🌐 `DELETE /carts/{cartId}` — Abandon a cart
- 🌐 `POST /carts/{cartId}/items` — Add an item to the cart
- 🌐 `PATCH /carts/{cartId}/items/{itemId}` — Update cart item quantity
- 🌐 `DELETE /carts/{cartId}/items/{itemId}` — Remove an item from the cart
- 🌐 `POST /carts/{cartId}/promo` — Apply a promo code to the cart
- 🌐 `DELETE /carts/{cartId}/promo` — Remove the applied promo code from the cart
- 🌐 `POST /checkout/sessions` — Start a checkout session and reserve stock
- 🌐 `GET /checkout/sessions/{sessionId}` — Get current checkout session state and totals
- 🌐 `PATCH /checkout/sessions/{sessionId}/address` — Set or update the delivery address step
- 🌐 `POST /checkout/sessions/{sessionId}/place-order` — Confirm the review step and create the order (pending payment)
- 🌐 `POST /payments/initiate` — Initiate a payment attempt for an order
- 🌐 `POST /payments/callback` — Receive payment outcome callback from provider
- 🌐 `POST /orders/{orderId}/payments/retry` — Retry payment for a failed or timed-out attempt
- 🔒 `GET /orders/{orderId}/payments` — List all payment attempts for an order [customer, staff, admin]
- 🔒 `GET /orders` — List orders for the current user (or all orders for staff/admin) [customer, staff, admin]
- 🔒 `GET /orders/{orderId}` — Get order detail including items and status history [customer, staff, admin]
- 🔒 `POST /orders/{orderId}/cancel` — Cancel an order and release reserved stock [customer, staff, admin]
- 🔒 `POST /orders/{orderId}/advance-status` — Advance order status through fulfilment stages (staff) [staff, admin]
- 🔒 `GET /orders/{orderId}/tracking` — Get simulated tracking events for an order [customer, staff, admin]
- 🔒 `GET /orders/{orderId}/status-history` — Get full status timeline for an order [customer, staff, admin]
- 🔒 `POST /orders/{orderId}/returns` — Customer submits a return request for an eligible item [customer]
- 🔒 `GET /orders/{orderId}/returns` — List return requests for an order [customer, staff, admin]
- 🔒 `GET /returns` — List all return requests (staff/admin) [staff, admin]
- 🔒 `GET /returns/{returnId}` — Get a return request by ID [customer, staff, admin]
- 🔒 `POST /returns/{returnId}/review` — Staff approves or rejects a return request [staff, admin]
- 🔒 `POST /orders/{orderId}/refunds` — Record a refund against an order (admin/staff) [staff, admin]
- 🔒 `GET /orders/{orderId}/refunds` — List refunds for an order [customer, staff, admin]
- 🔒 `GET /refunds/{refundId}` — Get a refund record by ID [customer, staff, admin]
- 🔒 `GET /promo-codes` — List all promo codes (merchandiser/admin) [merchandiser, admin]
- 🔒 `POST /promo-codes` — Create a promo code (merchandiser/admin) [merchandiser, admin]
- 🔒 `GET /promo-codes/{promoCodeId}` — Get a promo code by ID (merchandiser/admin) [merchandiser, admin]
- 🔒 `PATCH /promo-codes/{promoCodeId}` — Update a promo code (merchandiser/admin) [merchandiser, admin]
- 🔒 `DELETE /promo-codes/{promoCodeId}` — Delete a promo code (admin) [admin]
- 🌐 `POST /promo-codes/validate` — Validate a promo code and preview discount
- 🔒 `GET /notifications` — Poll in-app notifications for the current user [customer, staff, merchandiser, admin]
- 🔒 `GET /notifications/{notificationId}` — Get a single notification [customer, staff, merchandiser, admin]
- 🔒 `PATCH /notifications/{notificationId}` — Mark a notification as read [customer, staff, merchandiser, admin]
- 🔒 `PATCH /notifications/read-all` — Mark all notifications as read [customer, staff, merchandiser, admin]
- 🔒 `GET /admin/reports/summary` — Get consolidated business report summary [admin]
- 🔒 `GET /admin/reports/orders` — Get detailed orders report with period filter [admin]
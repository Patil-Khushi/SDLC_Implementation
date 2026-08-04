# Extracted Requirements

## Functional (56)
- **REQ-1** (Orders & Fulfilment): The system shall provide simulated tracking for orders.
- **REQ-2** (Orders & Fulfilment): The system shall allow customers to cancel an order.
- **REQ-3** (Orders & Fulfilment): When an order is cancelled before shipment, the system shall release the reserved stock.
- **REQ-4** (Orders & Fulfilment): The system shall allow staff to advance the status of orders from the same unified interface.
- **REQ-5** (Orders & Fulfilment): The system shall provide an order-detail page for each order.
- **REQ-6** (Orders & Fulfilment): The system shall support a returns flow consisting of a customer request step followed by an approval step.
- **REQ-7** (Orders & Fulfilment): The system shall allow a customer to request a return for eligible items within the return window.
- **REQ-8** (Orders & Fulfilment): The system shall provide customers with self-service capabilities to manage their orders.
- **REQ-9** (Orders & Fulfilment): The system shall support order cancellation.
- **REQ-10** (Orders & Fulfilment): Customers shall be able to view order status from their order page.
- **REQ-11** (Orders & Fulfilment): The order-detail page shall display a status timeline showing the stages: Confirmed, Packed, Shipped, and Delivered.
- **REQ-12** (Orders & Fulfilment): The system shall display order history to the user.
- **REQ-13** (Orders & Fulfilment): A refund shall be recorded against the originating order.
- **REQ-14** (Orders & Fulfilment): The system shall provide customers with an order history that includes a status timeline.
- **REQ-15** (Cart & Checkout): The system shall reserve stock when checkout starts.
- **REQ-16** (Cart & Checkout): The system shall check whether a given PIN code is serviceable for delivery.
- **REQ-17** (Cart & Checkout): The system shall release the stock reservation if payment does not complete within approximately 15 minutes of checkout starting.
- **REQ-18** (Cart & Checkout): The system shall display a clear message to the user when checkout is blocked due to an unserviceable PIN code.
- **REQ-19** (Cart & Checkout): The checkout flow shall collect the customer's delivery address as the first step.
- **REQ-20** (Cart & Checkout): The checkout flow shall present a review step displaying order totals and taxes as the third step.
- **REQ-21** (Cart & Checkout): The add-to-cart action shall resolve to a specific SKU based on the selected variant.
- **REQ-22** (Cart & Checkout): The system shall guide the customer through a short checkout flow to place an order.
- **REQ-23** (Cart & Checkout): The system shall allow a shopper to add items to the cart.
- **REQ-24** (Cart & Checkout): The system should allow guests to check out using only an email address and a delivery address, without requiring account creation.
- **REQ-25** (Cart & Checkout): The system shall allow a shopper to enter a promo code at checkout.
- **REQ-26** (Cart & Checkout): The entire checkout and order flow shall be fully buildable and testable without live payment keys, using only the test-mode mock.
- **REQ-27** (Cart & Checkout): The system shall display all charges (including totals and taxes) to the shopper before the payment step.
- **REQ-28** (Cart & Checkout): The system shall calculate and apply the correct shipping charge (₹49 or ₹0) to an order based on whether the order total meets the ₹799 free-shipping threshold.
- **REQ-29** (Cart & Checkout): After a guest completes checkout, the system should offer the guest an option to create an account.
- **REQ-30** (Cart & Checkout): The system shall provide a shorter checkout flow.
- **REQ-31** (Cart & Checkout): The system shall reflect the discount from an entered promo code in the order totals before the shopper completes payment.
- **REQ-32** (Payments): If payment fails, times out, or is pending, the customer's cart shall be preserved and the customer shall be able to retry payment.
- **REQ-33** (Payments): The platform shall include a provider-agnostic payment module.
- **REQ-34** (Payments): The payment module shall include a built-in test-mode mock that simulates success, failure, and pending payment outcomes.
- **REQ-35** (Account & Authentication): The system shall allow a customer to log in to their account securely.
- **REQ-36** (Account & Authentication): The system shall allow a new customer to register for an account using an email address and a password.
- **REQ-37** (Catalogue & Browsing): The product detail page shall provide a variant selector where each variant is its own SKU.
- **REQ-38** (Catalogue & Browsing): The product listing page shall allow users to apply multiple filters simultaneously, combining brand, price, and rating filters at once.
- **REQ-39** (Catalogue & Browsing): The product page shall display the price inclusive of tax.
- **REQ-40** (Catalogue & Browsing): The product listing page shall display the result count for each filter option.
- **REQ-41** (Catalogue & Browsing): The search functionality shall provide autocomplete suggestions as the user types.
- **REQ-42** (Catalogue & Browsing): The product detail page shall display a product image.
- **REQ-43** (Catalogue & Browsing): The variant picker on the product page shall resolve the selected size/colour combination to a specific SKU.
- **REQ-44** (Catalogue & Browsing): The system shall provide product search functionality.
- **REQ-45** (Catalogue & Browsing): The system shall let customers filter the product listing.
- **REQ-46** (Catalogue & Browsing): The system shall let customers browse the catalogue by category.
- **REQ-47** (Catalogue & Browsing): The system shall provide a product detail page that allows customers to view product information.
- **REQ-48** (Admin & Management): The system shall provide role-based access control so that staff members can only access the features and data permitted by their assigned role.
- **REQ-49** (Admin & Management): The system shall allow a merchandiser to create promo codes with either a percentage discount or a flat discount.
- **REQ-50** (Admin & Management): The system shall provide consolidated reporting for the business.
- **REQ-51** (Admin & Management): The system shall enforce role-based access control so that each team sees only the orders or order data it is permitted to see.
- **REQ-52** (Admin & Management): The system shall allow a merchandiser to set an expiry date on a promo code.
- **REQ-53** (Admin & Management): The system shall allow authorised staff to update catalogue content (e.g. products) without raising an engineering ticket.
- **REQ-54** (Admin & Management): The system shall provide a promotions engine.
- **REQ-55** (Admin & Management): The system shall provide a catalogue CMS enabling staff to manage product catalogue content.
- **REQ-56** (Notifications): The system shall simulate notifications via an in-app notification centre during the build.

## Performance (3)
- **NFR-1**: The site shall load and respond at a speed that users perceive as fast.
- **NFR-2**: Product listing and detail pages shall respond within approximately 2 seconds at the 95th percentile on a typical connection.
- **NFR-3**: The system shall provide faster browse performance.

## Non-Functional (9)
- **NFR-4** [security]: Passwords shall be stored using a strong one-way hashing algorithm (bcrypt or argon2) and must never be stored in plaintext.
- **NFR-5** [security]: Password-reset tokens shall expire after a defined period.
- **NFR-6** [security]: All staff and admin functions shall be protected by role-based access control.
- **NFR-7** [security]: Login and password-reset endpoints shall be rate-limited.
- **NFR-8** [security]: The platform shall not store any card data.
- **NFR-9** [security]: Login and logout shall be performed securely.
- **NFR-10** [software_quality]: The site shall be usable and user-friendly on mobile devices.
- **NFR-11** [software_quality]: Application errors shall be logged so that issues can be diagnosed.
- **NFR-12** [software_quality]: The key user flows — browse, product detail, cart, and checkout — shall meet basic WCAG AA accessibility requirements, including appropriate labels, sufficient colour contrast, and keyboard navigability.

## Business Rules (7)
- **BR-1**: Only eligible items may be available for return requests.
- **BR-2**: The system shall waive the shipping charge (free shipping) for orders with a total value of ₹799 or above.
- **BR-3**: Each size/colour combination on the product page shall be represented as its own distinct SKU with its own independent stock level.
- **BR-4**: An order shall not be confirmed until a payment callback explicitly confirms success.
- **BR-5**: The system shall apply GST tax calculations during checkout.
- **BR-6**: Vantage shall onboard the payment provider and perform KYC internally.
- **BR-7**: Prices displayed to the customer must be tax-inclusive.

## Constraints
- Live payment provider keys, merchant account, KYC, and PCI configuration are out of scope for the build; payments go live through configuration after the build is complete.
- The in-app search with autocomplete shall be included in the launch scope.
- Live payment keys, the merchant account, and real email/SMS delivery credentials are out of scope for the build and shall not be required to build, run, or test the system; they are supplied by Vantage post-build via configuration.
- Real email/SMS notification delivery is out of scope for the build; it is to be configured post-build only.
- Payments and tax are out of scope for this workstream and are handled as a separate thread with Finance and the vendor.
- Post-build configuration items handled by Vantage must not block implementation of the platform build.
- The payment module shall be provider-agnostic and expose a single unified interface.

## External Interfaces

## UI Token Source (feeds Design Tokens)
- ****: The customer-facing interface provides a product listing page with filters and result counts, a product detail page with variant selector and tax-inclusive pricing, a cart, a multi-step checkout flow, an order history and detail page with status timeline, and an in-app notification centre. The staff-facing interface is a unified admin panel providing catalogue CMS, promotions management, order management, and reporting, all gated by role-based access control. All interfaces must be mobile-responsive and keyboard-navigable with sufficient colour contrast.
- **3.1.1 Overall Visual Theme**: _Proposed design system — generated from the requirements; the raw inputs specify no visual design, so Design/UX to confirm before build._
- **3.1.1 Overall Visual Theme**: The product's visual personality is trustworthy, efficient, modern, approachable, clear. Indigo conveys the trust and reliability essential for a commerce platform handling secure accounts, payments, and role-based access, while orange injects the energy and warmth needed to drive conversions and create an approachable shopping experience across mobile and desktop. The design system is the single source of truth for the interface: every colour, type step and spacing value below is a NAMED token (never a hard-coded literal), so the look is consistent and re-themeable. The brand hue is used as an accent (~60/30/10 neutral/secondary/brand), not a flood, and status is always conveyed by an icon or label as well as colour.
- **3.1.2 Colour Tokens**: _Proposed design system — generated from the requirements; the raw inputs specify no visual design, so Design/UX to confirm before build._
- **3.1.3 Typography Tokens**: _Proposed design system — generated from the requirements; the raw inputs specify no visual design, so Design/UX to confirm before build._
- **3.1.4 Spacing, Radius, and Elevation Tokens**: _Proposed design system — generated from the requirements; the raw inputs specify no visual design, so Design/UX to confirm before build._
- **3.1.5 Layout and Interaction Standards**: _Proposed design system — generated from the requirements; the raw inputs specify no visual design, so Design/UX to confirm before build._
- **3.1.5 Layout and Interaction Standards**: Accessibility: all text meets WCAG 2.1 AA contrast (≥ 4.5:1 body, ≥ 3:1 large text / UI / borders); the token palette above is generated to satisfy this.
- **3.1.5 Layout and Interaction Standards**: Status is never colour alone: success/warning/error are always paired with an icon and a text label so colour-blind users are not excluded.
- **3.1.5 Layout and Interaction Standards**: Touch targets: interactive controls are at least 44 × 44 px with adequate spacing.
- **3.1.5 Layout and Interaction Standards**: Focus: every interactive element has a visible keyboard-focus indicator using color-primary.
- **3.1.5 Layout and Interaction Standards**: Responsive: layouts use the spacing scale and reflow from a single-column mobile view to multi-column desktop; content width is capped for readability.
- **3.1.5 Layout and Interaction Standards**: Motion: transitions are subtle and respect prefers-reduced-motion.

## UI Tokens (structured, from 3.1 tables)

### Colour (12)
- token: color-primary · hex: #4c6ef5 · usage: Primary brand colour — key actions, active states, links.
- token: color-primary-dark · hex: #3b5bdb · usage: Darker primary — button text-on-fill and hover/pressed states (AA).
- token: color-secondary · hex: #fd7e14 · usage: Secondary/accent — supporting highlights and secondary actions.
- token: color-success · hex: #37b24d · usage: Success status — confirmations, positive badges (with an icon/label).
- token: color-warning · hex: #fd7e14 · usage: Warning status — cautions (never colour alone).
- token: color-error · hex: #f03e3e · usage: Error status — validation errors, destructive actions.
- token: color-ink · hex: #212529 · usage: Primary text / headings on light surfaces.
- token: color-body · hex: #343a40 · usage: Body text on light surfaces.
- token: color-muted · hex: #495057 · usage: Secondary/muted text — captions, hints (AA-verified).
- token: color-surface · hex: #ffffff · usage: Card / panel surface.
- token: color-canvas · hex: #f8f9fa · usage: Page background (warm off-white).
- token: color-border · hex: #868e96 · usage: Borders, dividers, input outlines (AA 3:1 vs canvas).

### Typography (7)
- token: type-display · size / line-height: 32px / 40px · weight: 700 · usage: Page or hero titles.
- token: type-h1 · size / line-height: 24px / 32px · weight: 700 · usage: Section headings.
- token: type-h2 · size / line-height: 20px / 28px · weight: 600 · usage: Sub-section headings.
- token: type-h3 · size / line-height: 16px / 24px · weight: 600 · usage: Card / group headings.
- token: type-body · size / line-height: 16px / 24px · weight: 400 · usage: Default body text.
- token: type-small · size / line-height: 14px / 20px · weight: 400 · usage: Secondary text, metadata.
- token: type-caption · size / line-height: 12px / 16px · weight: 400 · usage: Captions, helper text, labels.

### Spacing Radius And Elevation (12)
- token: space-1 · value: 4px · usage: Tight gaps (icon-to-label).
- token: space-2 · value: 8px · usage: Compact padding, chips.
- token: space-3 · value: 12px · usage: Control padding.
- token: space-4 · value: 16px · usage: Default element spacing.
- token: space-6 · value: 24px · usage: Card padding, group spacing.
- token: space-8 · value: 32px · usage: Section spacing.
- token: radius-sm · value: 6px · usage: Inputs, chips.
- token: radius-md · value: 10px · usage: Buttons, cards.
- token: radius-lg · value: 16px · usage: Modals, sheets.
- token: elevation-1 · value: 0 1px 2px rgba(0,0,0,.06) · usage: Cards at rest.
- token: elevation-2 · value: 0 4px 12px rgba(0,0,0,.10) · usage: Dropdowns, popovers.
- token: elevation-3 · value: 0 12px 28px rgba(0,0,0,.14) · usage: Modals, dialogs.

## Tech Stack

### 7 Technology Stack
- Proposed technology stack — generated from the requirements; to be confirmed by Design / Engineering. Each layer names the option selected on the review screen (the recommended default unless a reviewer changed it).
- Selected stack: React.js + Vite + Tailwind CSS · Node.js + Express · MySQL 8 · JWT + bcrypt · In-app admin module · Elasticsearch · In-app notification store (PostgreSQL + REST polling).

### 7.1 Client Applications
- React.js + Vite + Tailwind CSS — Needs fast page loads, mobile usability, autocomplete search, multi-filter PLP, SKU variant picker, short checkout flow, and an in-app notification centre. Specified by the reviewer (custom entry).

### 7.2 Backend Architecture
- Node.js + Express — Needs a REST API covering orders, cart, checkout, returns, promotions engine, RBAC, payment abstraction with test-mode mock, and guest checkout logic. Widely known, minimal-overhead HTTP framework that shares the JavaScript ecosystem with the Next.js frontend, making it easy to hire for and build the full feature set quickly.

### 7.3 Data Storage
- MySQL 8 — Relational data model is required for orders, SKUs (size/colour combinations with independent stock), refunds linked to orders, promotions, and RBAC roles. Equally mainstream relational database with wide hosting support and good performance, but PostgreSQL's JSONB and full-text capabilities are more useful for product catalogue flexibility.

### 7.4 Third-Party Integrations
- In-app admin module — Staff must update product catalogue content without raising engineering tickets, requiring a headless CMS with a staff-friendly UI that feeds the API. Specified by the reviewer (custom entry).
- Elasticsearch — Requires autocomplete suggestions, multi-filter PLP (brand, price, rating) with per-option result counts, and category browsing — all within the ~2s response target. Purpose-built distributed search engine with native faceted aggregations and autocomplete, but adds significant operational overhead that is only justified if catalogue size or query complexity outgrows PostgreSQL.
- In-app notification store (PostgreSQL + REST polling) — Live email/SMS delivery is out of scope for the build; the system must simulate notifications via an in-app notification centre during the build phase. A notifications table in PostgreSQL with a REST endpoint polled by the frontend delivers the in-app notification centre with zero additional infrastructure and is trivially swappable for real email/SMS post-build.

### 7.5 Security Technologies
- JWT + bcrypt — Requires customer registration with email/password (bcrypt/argon2), guest checkout, RBAC for staff roles, and secure session management. Stateless JWT access tokens with bcrypt password hashing is a simple, well-understood pattern that directly satisfies the password storage requirement and RBAC claims without additional infrastructure.

### 7.6 Device Capabilities
- The client experiences are responsive web applications, so no native device capabilities (camera, GPS, push, biometrics) are required for this release beyond standard browser APIs. Native-app device features are out of scope until dedicated mobile apps are introduced.
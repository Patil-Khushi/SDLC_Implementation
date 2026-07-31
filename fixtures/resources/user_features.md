# User Features

## Roles

## Entities


## Features

### 4.1 Orders & Fulfilment  ·  Priority: Medium
_Requirements grouped under the "Orders & Fulfilment" capability_
- **Story (derived):** As a customer, I want orders & fulfilment.
- **Flow:**
    1. 1. Stimulus — A customer navigates to their order history and selects an order.
    2. Response — The system displays the order-detail page, including a status timeline showing the stages Confirmed, Packed, Shipped, and Delivered, along with simulated tracking information.
    3. 2. Stimulus — A customer requests cancellation of an order that has not yet been shipped.
    4. Response — The system cancels the order, releases the reserved stock, and records the cancellation against the order.
    5. 3. Stimulus — A customer submits a return request for an eligible item within the return window.
    6. Response — The system records the return request and routes it for staff approval; upon approval, a refund is recorded against the originating order.
    7. 4. Stimulus — A staff member advances the status of an order from the unified admin interface.
    8. Response — The system updates the order status, reflects the new stage on the customer's status timeline, and enforces that only staff with the appropriate role can perform this action.
- **Requirements:** REQ-1, REQ-2, REQ-3, REQ-4, REQ-5, REQ-6, REQ-7, REQ-8, REQ-9, REQ-10, REQ-11, REQ-12, REQ-13, REQ-14

### 4.2 Cart & Checkout  ·  Priority: High
_Requirements grouped under the "Cart & Checkout" capability_
- **Story (derived):** As a customer, I want cart & checkout.
- **Flow:**
    1. 1. Stimulus — A shopper selects a variant on the product detail page and clicks Add to Cart.
    2. Response — The system resolves the selected size/colour combination to a specific SKU and adds that SKU to the shopper's cart.
    3. 2. Stimulus — A customer begins checkout by submitting their delivery address and PIN code.
    4. Response — The system checks whether the PIN code is serviceable; if serviceable, it reserves stock and advances the customer to the payment details step. If the PIN code is unserviceable, the system displays a clear blocking message and does not proceed.
    5. 3. Stimulus — The customer reaches the review step and submits a promo code, then proceeds to payment.
    6. Response — The system applies the promo code discount, calculates GST, applies the correct shipping charge (₹0 if order total ≥ ₹799, otherwise ₹49), displays all charges including totals and taxes, and presents the payment step.
    7. 4. Stimulus — Payment does not complete within approximately 15 minutes of checkout starting.
    8. Response — The system releases the stock reservation, preserves the customer's cart, and allows the customer to retry payment.
- **Requirements:** REQ-15, REQ-16, REQ-17, REQ-18, REQ-19, REQ-20, REQ-21, REQ-22, REQ-23, REQ-24, REQ-25, REQ-26, REQ-27, REQ-28, REQ-29, REQ-30, REQ-31

### 4.3 Payments  ·  Priority: High
_Requirements grouped under the "Payments" capability_
- **Story (derived):** As a customer, I want payments.
- **Flow:**
    1. 1. Stimulus — The customer submits payment at the checkout payment step (test mode).
    2. Response — The system routes the payment through the provider-agnostic payment module using the built-in test-mode mock, which simulates a success outcome; upon a success callback, the system confirms the order.
    3. 2. Stimulus — The test-mode mock returns a failure or pending outcome for a payment attempt.
    4. Response — The system does not confirm the order, preserves the customer's cart, and presents the customer with the option to retry payment.
    5. 3. Stimulus — A payment callback explicitly confirms success.
    6. Response — The system marks the order as confirmed; no card data is stored by the platform at any point during this flow.
- **Requirements:** REQ-32, REQ-33, REQ-34

### 4.4 Account & Authentication  ·  Priority: Medium
_Requirements grouped under the "Account & Authentication" capability_
- **Story (derived):** As a customer, I want account & authentication.
- **Flow:**
    1. 1. Stimulus — A new visitor submits a registration form with an email address and a password.
    2. Response — The system creates the account, stores the password using bcrypt or argon2 (never plaintext), and logs the customer in.
    3. 2. Stimulus — A registered customer submits their email and password on the login page.
    4. Response — The system authenticates the credentials securely, establishes a session, and redirects the customer to their account.
    5. 3. Stimulus — A customer requests a password reset and submits the reset form.
    6. Response — The system issues a time-limited reset token; the token expires after a defined period, and the login/password-reset endpoint enforces rate limiting to prevent abuse.
- **Requirements:** REQ-35, REQ-36

### 4.5 Catalogue & Browsing  ·  Priority: High
_Requirements grouped under the "Catalogue & Browsing" capability_
- **Story (derived):** As a customer, I want catalogue & browsing.
- **Flow:**
    1. 1. Stimulus — A customer types a search query into the search bar.
    2. Response — The system provides autocomplete suggestions as the user types and returns matching products when the query is submitted.
    3. 2. Stimulus — A customer applies multiple filters (brand, price, rating) simultaneously on the product listing page.
    4. Response — The system combines all selected filters, returns the filtered result set, and displays the result count for each active filter option.
    5. 3. Stimulus — A customer opens a product detail page and selects a size and colour combination.
    6. Response — The system resolves the selection to a specific SKU, displays the product image, shows the tax-inclusive price, and enables the Add to Cart action for that SKU.
- **Requirements:** REQ-37, REQ-38, REQ-39, REQ-40, REQ-41, REQ-42, REQ-43, REQ-44, REQ-45, REQ-46, REQ-47

### 4.6 Admin & Management  ·  Priority: High
_Requirements grouped under the "Admin & Management" capability_
- **Story (derived):** As a platform administrator, I want admin & management.
- **Flow:**
    1. 1. Stimulus — An authorised merchandiser logs in and navigates to the catalogue CMS to create or update a product.
    2. Response — The system verifies the user's role permits catalogue management, saves the product content, and makes it available on the storefront without requiring an engineering ticket.
    3. 2. Stimulus — A merchandiser creates a promo code, specifying a percentage or flat discount and an expiry date.
    4. Response — The system records the promo code with the specified discount type and expiry date and makes it available for use at checkout.
    5. 3. Stimulus — A fulfilment staff member attempts to access order data outside their permitted scope.
    6. Response — The system enforces role-based access control and denies access, displaying only the orders and data the staff member's role permits.
    7. 4. Stimulus — An administrator requests the consolidated report.
    8. Response — The system generates and displays the consolidated business report, restricted to users whose role grants reporting access.
- **Requirements:** REQ-48, REQ-49, REQ-50, REQ-51, REQ-52, REQ-53, REQ-54, REQ-55

### 4.7 Notifications  ·  Priority: Medium
_Requirements grouped under the "Notifications" capability_
- **Story (derived):** As a customer, I want notifications.
- **Flow:**
    1. 1. Stimulus — An order event (e.g. order confirmed, status change) occurs for a customer.
    2. Response — The system records the notification and makes it available in the customer's in-app notification centre; no real email or SMS is sent during the build.
    3. 2. Stimulus — The customer opens the in-app notification centre.
    4. Response — The system displays all pending and historical notifications for that customer's account.
- **Requirements:** REQ-56
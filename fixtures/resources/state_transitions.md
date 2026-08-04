Home:
- Loading → Full-width skeleton: hero banner placeholder (grey rectangle, ~40vh), row of category-chip skeletons, grid of product-card skeletons (8 cards, image + two text lines each) shimmer-animated against color-canvas background.
- Empty   → N/A — Home is a curated static/editorial page; it always renders hero, category links, and featured products seeded by the CMS; a truly empty catalogue is treated as a content configuration issue, not a runtime empty state.
- Error   → Full-page error panel centred on color-canvas: error icon (color-error) + heading "Something went wrong" (type-h1) + body "We couldn't load the page right now. Please refresh or try again shortly." (type-body) + "Refresh" primary button. Errors are logged per NFR-11.
- Success → Hero banner with brand imagery; horizontal scrollable category-chip row (links to /categories/:slug/products); featured product grid (cards showing primary image, product name, tax-inclusive price in ₹, average_rating stars); global search bar with autocomplete input (REQ-41).

Product Listing:
- Loading → Sidebar filter-panel skeleton (brand, price, rating group placeholders) on left; right area shows result-count placeholder + grid of 12 product-card skeletons (image block + two text-line shimmer bars each).
- Empty   → Sidebar filters rendered and interactive; main area shows empty-state illustration + "No products found" heading (type-h1) + "Try adjusting your filters or browsing a category." (type-body) + "Clear all filters" link.
- Error   → Sidebar hidden; centred error panel: error icon + "Couldn't load products" heading + "Please try again." message + "Retry" primary button.
- Success → Left sidebar with collapsible filter groups for Brand (checkbox list with per-option result counts), Price range (min/max inputs or range slider), and Rating (star-level checkboxes with counts) per REQ-38/REQ-40; multiple filters combinable simultaneously; top bar shows total result count and sort control; responsive product grid of cards each showing primary image, product name, brand, tax-inclusive price (₹), average rating — all linking to /products/:slug.

Product Listing by Category:
- Loading → Category heading placeholder (single shimmer line, type-h1 height); then same sidebar + grid skeleton as Product Listing page.
- Empty   → Category name rendered as heading; sidebar shown; main area: "No products in this category yet" (type-h1) + "Check back soon or browse other categories." (type-body) + "Browse all products" link to /products.
- Error   → If category slug is not found in categories table: "Category not found" (type-h1) + "The category you're looking for doesn't exist." (type-body) + "Browse all products" link. If DB/API error: same error panel as Product Listing error state.
- Success → Category name as page heading (type-h1); breadcrumb trail reflecting parent_id hierarchy; same filterable, multi-filter product grid as Product Listing scoped to the resolved category_id; per-option result counts updated for the category scope.

Search Results:
- Loading → Search bar pre-filled with query; below: row of autocomplete-suggestion skeletons (3–5 shimmer lines); then grid of product-card skeletons matching Product Listing loading pattern.
- Empty   → Search bar with submitted query; "No results for '[query]'" heading (type-h1, color-ink); suggestions: "Check your spelling or try a broader search term." (type-body, color-muted); "Browse all products" secondary CTA link to /products.
- Error   → Search bar retains query; centred error panel: error icon + "Search unavailable" heading + "We couldn't complete your search. Please try again." body + "Retry" button.
- Success → Search bar pre-populated with query; total result count label (e.g. "42 results for 'running shoes'"); product grid cards (image, name, brand, tax-inclusive price, rating) identical in structure to Product Listing; autocomplete suggestions rendered as dropdown overlay while user is typing per REQ-41; filter sidebar available for further refinement (brand, price, rating with counts).

Product Detail:
- Loading → Skeleton layout: left half — large image placeholder rectangle; right half — product-name shimmer (type-h1 height), price shimmer, variant-picker chip-row skeletons (two rows: size, colour), "Add to Cart" button disabled/ghost state.
- Empty   → N/A — if a slug resolves to no product (is_active = false or non-existent), the page renders a Not Found state (see Error) rather than an empty content state.
- Error   → If product not found or inactive: "Product not found" heading (type-h1) + "This product is no longer available." (type-body) + "Browse products" link to /products. If API error: standard error panel with retry button. Both cases show icon + label per WCAG requirement.
- Success → Primary product image (product_images.is_primary, with alt_text); image gallery thumbnails if multiple images; product name (type-h1), brand name, tax-inclusive price in ₹ (base_price_paise + tax computed, per BR-7/REQ-39); variant selector — size chips and colour chips resolving to a specific SKU per BR-3/REQ-37/REQ-43; selected SKU stock availability indicator ("In stock" / "Out of stock" based on stock_quantity − reserved_quantity > 0); "Add to Cart" primary button (disabled if SKU out of stock or no variant selected) per REQ-23; product description; return window notice (e.g. "7-day returns" from products.return_window_days) if is_returnable = true.

Cart:
- Loading → Cart heading; skeleton rows for 2–3 line items (image thumbnail placeholder + product-name shimmer + quantity control ghost + price shimmer); order-summary panel skeleton on right (subtotal, shipping, total shimmer lines).
- Empty   → Cart icon illustration; "Your cart is empty" heading (type-h1); "Looks like you haven't added anything yet." body (type-body, color-muted); "Start shopping" primary CTA button linking to /products.
- Error   → Centred error panel: error icon + "Couldn't load your cart" heading + "Please refresh the page." body + "Refresh" button. If add/remove/quantity-change action fails inline, show a toast notification (color-error, icon + label) without destroying existing cart view.
- Success → List of cart_items: each row shows product image thumbnail, product_name_snapshot, variant details (size/colour), quantity stepper (+/−), unit_price in ₹, line total, and "Remove" link. Order summary panel: subtotal, promo code input field (REQ-25) with "Apply" button showing discount_paise deducted and confirmation/error message, shipping charge (₹49 or ₹0 per BR-2/REQ-28) with free-shipping progress indicator if below ₹799 threshold, GST amount, grand total. "Proceed to Checkout" primary button linking to /checkout/address. Guest users see same cart; registered users see saved cart state.

Checkout - Address:
- Loading → Step-progress indicator (Step 1 of 3: Address — Payment — Review) with step 1 active; below: form skeleton (shimmer blocks for name, address lines, city, state, PIN, phone fields); "Continue" button disabled.
- Empty   → N/A — the address form is always rendered with blank fields for new input; no data fetch is required to display the form itself (saved addresses for registered users are a progressive enhancement fetched separately, which has its own inline loading state).
- Error   → Inline field-level validation errors (color-error, icon + label beneath each invalid field) for required fields (full_name, line1, city, state, pin_code). PIN code serviceability error: prominent banner below the PIN field — "Sorry, we don't deliver to this PIN code yet." (color-error icon + text, per REQ-18) — checkout blocked, "Continue" button disabled. API/network error on submission: toast error notification; form data preserved so user does not lose input.
- Success → Step-progress indicator showing step 1 active; form fields: Full Name, Address Line 1, Address Line 2 (optional), City, State, PIN Code, Phone; registered users see a "Saved addresses" dropdown to prefill the form; real-time PIN code serviceability check on blur of PIN field (queries serviceable_pin_codes) with success indicator ("✓ Delivery available", color-success icon + label) when serviceable; "Continue to Payment" primary button advances to /checkout/payment on valid serviceable submission; stock reservation is created at this point per REQ-15.

Checkout - Payment:
- Loading → Step-progress indicator step 2 active; payment method panel skeleton (shimmer rectangle for mock payment widget area); order summary sidebar skeleton (subtotal/total shimmer lines); "Pay Now" button disabled.
- Empty   → N/A — payment step always shows the mock payment interface; there is no state where payment options are absent.
- Error   → Payment failure outcome from test-mode mock (payment_attempts.status = 'failed' or 'timed_out'): error banner — "Payment unsuccessful. [failure_reason if available]" with error icon + label; cart is preserved per REQ-32; "Retry Payment" primary button re-initiates a new payment_attempt; stock reservation expiry countdown shown if nearing 15-minute limit per REQ-17. Network/API error: toast error with retry option.
- Success → Step-progress indicator step 2 active; test-mode badge indicator ("Test Mode" chip, color-warning, icon + label per REQ-26); mock payment widget with simulated outcome selector (Success / Failure / Pending) per REQ-34; order summary sidebar showing subtotal, discount (if promo applied), shipping charge (₹0 or ₹49), GST, and grand total (all charges visible before payment per REQ-27); "Pay ₹[total]" primary CTA button; on success callback, order current_status transitions from 'pending_payment' to 'confirmed' per BR-4, and user is redirected to /checkout/confirmation.

Checkout - Review:
- Loading → Step-progress indicator step 3 active (Address — Payment — Review); skeleton blocks for delivery address summary, line-item list, and pricing breakdown panel; promo code input skeleton; "Confirm & Pay" button disabled.
- Empty   → N/A — if a user reaches /checkout/review with an empty or abandoned cart, they are redirected to /cart automatically; the review step always has order data to display.
- Error   → If promo code entered is invalid, expired (expires_at < now()), exceeded max_uses, or below minimum_order_paise: inline error beneath promo field — "Invalid or expired promo code." (color-error icon + label); promo discount not applied. If order total recalculation fails on API: toast error, existing displayed values preserved, "Confirm" button disabled until resolved.
- Success → Step-progress indicator step 3 active; delivery address summary (from address snapshot); itemised order lines (product_name_snapshot, variant, quantity, unit price, line total); promo code input with "Apply" button; pricing breakdown: subtotal, promo discount (if applicable, shown as − ₹X), shipping charge (₹0 or ₹49 per BR-2), GST amount (per BR-5), grand total — all displayed before payment per REQ-20/REQ-27; "Edit Address" and "Edit Cart" links; "Confirm & Proceed to Payment" primary button advances to /checkout/payment.

Checkout - Confirmation:
- Loading → Centred panel skeleton: large checkmark placeholder circle, order-number shimmer line, summary shimmer lines; no navigation buttons until loaded.
- Empty   → N/A — confirmation page is only reachable after a successful payment callback sets order current_status = 'confirmed'; there is no empty data scenario.
- Error   → If order cannot be retrieved by order_number after redirect: error panel — "We couldn't retrieve your order confirmation." + "Check your order history or contact support." + "Go to Order History" link. Error logged per NFR-11.
- Success → Large success icon (color-success, icon + label "Order Confirmed!") per WCAG requirement; order_number displayed prominently (type-h1); estimated delivery information; itemised order summary (product names, quantities, totals); delivery address snapshot; "Continue Shopping" secondary CTA to /products; if user is a guest (guest_email present, user_id null): prominent "Create an account to track your order" CTA linking to /checkout/register per REQ-29; registered users see "View Order" link to order detail page.

Guest Post-Checkout Register:
- Loading → N/A — the registration form itself is static markup; no data fetch is required before rendering the form.
- Empty   → N/A — the form is always fully rendered; no empty-data scenario applies to a registration form.
- Error   → Inline field errors: password field validation (minimum length, strength hint); email already registered: "An account with this email already exists. Log in instead?" (color-error icon + label) with link to /login; API/network error on submission: toast error, form data preserved. Rate-limiting error: "Too many attempts. Please wait before trying again." per NFR-7.
- Success → Page heading "Save your details for faster checkout" (type-h1); guest's email pre-filled and read-only (sourced from orders.guest_email); password field with strength indicator; "Create Account" primary button; "Skip for now — continue as guest" secondary text link back to /checkout/confirmation; on successful account creation: bcrypt-hashed password stored per NFR-4, is_guest_converted set to true on users record, user logged in via JWT per REQ-36, redirected to order history or confirmation.

Login:
- Loading → N/A — the login form is static markup rendered immediately; no data fetch is required before the form is usable.
- Empty   → N/A — the login form always renders fully with email and password fields; there is no empty-data scenario.
- Error   → Inline error beneath form (not field-specific, to avoid username enumeration): "Incorrect email or password." (color-error icon + label); rate-limit error: "Too many login attempts. Please try again later." per NFR-7; network/API error: toast notification with "Try again" option. All errors paired with icon and text label per WCAG/design-system rule.
- Success → Page heading "Welcome back" (type-h1); email input field; password input field with show/hide toggle; "Sign in" primary button; "Forgot password?" link (triggers rate-limited password-reset flow per NFR-5/NFR-7); "New customer? Create an account" link to registration; on successful authentication: JWT issued per REQ-35, user redirected to their intended destination (cart, checkout, or account home); guest-checkout option link to /checkout/address for users who prefer not to log in per REQ-24.

Register:
- Loading → N/A — static registration form with no data fetch required before render
- Empty   → N/A — form fields (full name, email, password, confirm password) are initially blank by design
- Error   → Inline field-level validation messages in color-error (e.g. "Email already in use", "Passwords do not match", "Password required"); rate-limit error banner above the form if the endpoint rejects the request; network/server error toast with icon + label per WCAG AA colour-alone rule
- Success → User is created, JWT session established, and the customer is redirected to /account; no persistent success state remains on this page

Forgot Password:
- Loading → N/A — static single-field form; no data fetch needed to render
- Empty   → N/A — email input field is blank by design on arrival
- Error   → Inline error message in color-error below the email field for invalid or unrecognised format; rate-limit warning banner (icon + text) shown when the endpoint rejects further attempts per NFR-7; generic server error banner for 5xx responses
- Success → Confirmation panel replaces the form: icon (✓) + heading "Check your inbox" + body text explaining a reset link has been sent (no actual email delivered during build per constraints); no email address is leaked in the message

Reset Password:
- Loading → Skeleton or spinner shown while the API validates the reset token from the URL query parameter before rendering the form
- Empty   → N/A — new-password and confirm-password fields are blank by design once the token is validated
- Error   → Full-page error state if the token is missing, invalid, or expired (per NFR-5): icon + "This reset link is invalid or has expired" message + link to /forgot-password; inline field validation errors (color-error + icon) for mismatched or weak passwords; server/network error banner for 5xx
- Success → Password updated confirmation panel: icon (✓) + "Password reset successfully" message + link to /login; session is not auto-established, customer must log in

Account Overview:
- Loading → Skeleton cards for the summary panels (recent orders count, saved addresses count, notification badge count) while user profile and order summary data are fetched
- Empty   → Rendered overview with zero-state callouts: "No orders yet — start shopping" with a link to the catalogue; "No saved addresses" with a link to /account/addresses/new; all nav links to sub-sections remain fully accessible
- Error   → Error banner (icon + "Unable to load your account details. Please try again.") with a retry button; nav links to sub-pages remain rendered so the customer is not fully blocked
- Success → Welcome heading with user's full_name, summary tiles showing order count (from orders table filtered by user_id), saved address count, unread notification count (from notifications.is_read = false); quick-links to Order History, Profile, Addresses, and Notifications

Account Profile:
- Loading → Skeleton input fields while the user record (full_name, email, phone from the users table) is fetched
- Empty   → N/A — fields are pre-populated from the authenticated user record; a user always exists at this route
- Error   → Error banner with icon + "Unable to load profile. Please try again." and retry button if the GET fails; on save, inline error per field in color-error plus a form-level error banner if the PATCH/PUT fails (e.g. email already taken); rate-limit error for password-change attempts per NFR-7
- Success → Editable form showing full_name, email (read-only or change-flow), phone; save button triggers PATCH; on successful save, a transient success toast (icon + "Profile updated") appears and form reflects the saved values

Account Addresses:
- Loading → Skeleton address cards while the addresses list (filtered by user_id) is fetched from the addresses table
- Empty   → Illustration or icon + "No saved addresses yet" message + prominent "Add address" button linking to /account/addresses/new
- Error   → Error banner (icon + "Unable to load your addresses. Please try again.") with retry button; "Add address" button still rendered
- Success → List of address cards, each showing full_name, line1, line2, city, state, pin_code, phone; default address visually badged ("Default"); each card has Edit and Delete actions; a primary "Add address" button links to /account/addresses/new

Account Add Address:
- Loading → N/A — static form; no data fetch required before rendering the blank address form
- Empty   → N/A — all fields (full_name, line1, line2, city, state, pin_code, country, phone, is_default toggle) are blank by design
- Error   → Inline field validation errors (color-error + icon) for required fields (full_name, line1, city, state, pin_code); PIN code serviceability check failure shown as an inline warning (icon + "Delivery to this PIN code is not available") per REQ-16/REQ-18; server error banner on save failure
- Success → Address record created in addresses table linked to user_id; customer is redirected to /account/addresses with the new address appearing in the list; if is_default was checked, previous default is cleared

Account Edit Address:
- Loading → Skeleton input fields while the specific address record (by addresses.id and user_id) is fetched
- Empty   → N/A — fields are pre-populated from the fetched address record
- Error   → Full-page "Address not found" state (icon + message + back link) if the address id does not exist or does not belong to the authenticated user; inline field validation errors (color-error + icon) on save; PIN code unserviceable inline warning per REQ-16/REQ-18; server error banner on PATCH failure
- Success → Editable form pre-filled with existing address values; on successful save, customer is redirected to /account/addresses with updated address card reflecting the changes; default badge updated if is_default was toggled

Order History:
- Loading → Skeleton list of order summary rows (order number, date, status badge, total) while orders are fetched from the orders table filtered by user_id, ordered by created_at DESC
- Empty   → Icon + "You haven't placed any orders yet" message + "Start shopping" link to the product listing page
- Error   → Error banner (icon + "Unable to load your orders. Please try again.") with retry button
- Success → Paginated list of order cards each showing: order_number, created_at (formatted date), current_status as a colour + icon badge (Confirmed / Packed / Shipped / Delivered / Cancelled / etc.), total_paise displayed as ₹ value (divided by 100), and a "View details" link to /account/orders/:id; status is never conveyed by colour alone per design standards

Order Detail:
- Loading → Skeleton for the status timeline, order item rows, and price summary panel while order data (orders, order_items, order_status_history, order_tracking) is fetched
- Empty   → N/A — an order either exists or triggers an error; no meaningful empty state within a valid order
- Error   → Full-page "Order not found" state (icon + message + back link to /account/orders) if the order id does not exist or does not belong to the authenticated user/guest_email; server error banner with retry for 5xx responses
- Success → Order number and date header; status timeline with four stages (Confirmed → Packed → Shipped → Delivered) showing completed stages as filled, current stage as active, future stages as inactive — each stage labelled with icon + text and timestamp from order_status_history; simulated tracking events list from order_tracking (event_label, event_description, location, occurred_at); order items table (product_name_snapshot, variant_snapshot size/colour, quantity, unit_price_paise as ₹); price summary (subtotal, discount if promo_code_snapshot present, shipping_charge_paise, tax_paise, total_paise all in ₹); delivery address from delivery_address_snapshot; "Cancel order" button if current_status is confirmed or packed (pre-shipment); "Request return" button per eligible order_items (is_returnable = true, within return_window_days); any associated refund status if current_status is refunded

Return Request:
- Loading → Skeleton for the eligible items selector and reason field while order_items (filtered by order_id and is_returnable = true) and return_window_expires_at are fetched
- Empty   → "No eligible items for return" message (icon + explanation that items are either non-returnable or outside the return window) + back link to /account/orders/:id; rendered when no order_items satisfy is_returnable = true and return_window_expires_at > now()
- Error   → Full-page error if the order does not belong to the authenticated user or does not exist (icon + message + back link); form-level error banner if the return request POST fails (server error); inline error if the return window has expired at submission time ("Return window for this item has closed")
- Success → Form showing eligible item(s) with checkbox selection, a reason text area, and a submit button; on successful submission, a return_requests record is created with status = 'requested', and a confirmation panel is shown: icon (✓) + "Return request submitted" + "Our team will review your request" message + link back to /account/orders/:id; order status on parent page will subsequently reflect return_requested

Notifications:
- Loading → Skeleton notification rows (title placeholder, body placeholder, timestamp placeholder) while notifications are fetched from the notifications table filtered by user_id (or guest_email), ordered by created_at DESC
- Empty   → Icon + "You're all caught up — no notifications yet" message; displayed when the notifications query returns zero rows for the authenticated user
- Error   → Error banner (icon + "Unable to load notifications. Please try again.") with retry button
- Success → Chronological list of notification items each showing: title (type-h3), body text (type-body), relative or formatted created_at timestamp (type-caption, color-muted); unread items visually distinguished with a left accent bar or bold title (is_read = false); marking an item as read updates is_read to true via REST PATCH; unread count badge in page heading reflects current unread total; notifications cover order events (order confirmed, status changes, return approved, refund processed) as simulated in-app records per REQ-56

Admin Dashboard:
- Loading → Full-page skeleton showing stat-card placeholders (4 cards in a row: Total Orders, Revenue, Pending Returns, Active Promo Codes), a skeleton bar chart for recent order volume, and a skeleton recent-orders table; top nav and sidebar render immediately with the authenticated staff member's name and role badge.
- Empty   → N/A — the dashboard always has system-level aggregate data (even zero values are meaningful); stat cards display "0" with labels, chart renders an empty axis, and the recent-orders table shows "No orders yet" with a prompt to check back later.
- Error   → Inline error banner (color-error + icon + label) beneath the page heading reading "Dashboard data could not be loaded. Please refresh or contact support."; stat cards show "—" placeholders; sidebar navigation remains functional so staff can navigate elsewhere.
- Success → Four summary stat cards (Total Orders count, Gross Revenue in ₹, Pending Return Requests count, Active Promo Codes count) drawn from orders, return_requests, and promo_codes tables; a recent-orders table showing the last 10 orders with order_number, customer email, current_status badge, total_paise formatted as ₹, and created_at date; a role-restricted notice if the user's role does not permit reporting (REQ-51); all sections gated by RBAC so fulfilment staff see only order counts, not revenue.

Admin Order List:
- Loading → Skeleton table rows (10 rows × 6 column placeholders) with a skeleton pagination strip and skeleton filter/search bar above; page heading "Orders" renders immediately.
- Empty   → Table renders with headers (Order #, Customer, Status, Items, Total, Date) and a centred empty-state message "No orders found" with a secondary label "Try adjusting your filters"; filter/search controls remain active.
- Error   → Error banner (color-error + icon + label) above the table reading "Orders could not be loaded. Please try again."; retry button triggers a fresh fetch; table body shows no rows.
- Success → Paginated table of orders from the orders table showing: order_number, customer email (user email or guest_email), current_status as a colour-and-icon badge (confirmed / packed / shipped / delivered / cancelled / return_requested / return_approved / refunded), item count from order_items, total_paise formatted as ₹, created_at date; filter controls for status, date range, and keyword search; each row links to Admin Order Detail; staff see only orders permitted by their role (REQ-51, NFR-6); "Advance Status" quick-action button per row for eligible statuses (REQ-4).

Admin Order Detail:
- Loading → Skeleton layout: order header placeholder, skeleton status-timeline strip (4 stage pills), skeleton order-items table, skeleton delivery address card, skeleton financial summary card; breadcrumb and back link render immediately.
- Empty   → N/A — the page is reached via a valid order ID; if the ID resolves to nothing, this falls to the Error state.
- Error   → Full-width error banner reading "Order not found or you do not have permission to view it." (covers both 404 and RBAC denial per REQ-51); back-to-list link displayed; no partial data shown.
- Success → Order header with order_number, current_status badge, created_at date, and guest_email or linked user email; status timeline showing the four stages Confirmed → Packed → Shipped → Delivered as sequential step indicators driven by order_status_history records, with completed stages highlighted and timestamps (REQ-11); simulated tracking events list from order_tracking (event_label, event_description, location, occurred_at) (REQ-1); order items table with product_name_snapshot, variant_snapshot (size/colour), sku_code_snapshot, quantity, unit_price_paise (÷100 as ₹), tax_paise; financial summary showing subtotal, discount (promo_code_snapshot if applied), shipping_charge_paise, tax_paise, total_paise all in ₹; delivery address from delivery_address_snapshot; "Advance Status" button for staff with appropriate role (REQ-4); "Cancel Order" button visible when current_status is confirmed or packed (REQ-2); return_requests panel listing any return requests for order items with status badges and "Approve / Reject" actions for eligible staff (REQ-6); refunds panel listing any records from refunds table linked to this order (REQ-13).

Admin Catalogue Products:
- Loading → Skeleton grid or table of product rows (8 placeholder cards/rows) with skeleton search bar and "New Product" button; page heading "Products" renders immediately.
- Empty   → Empty-state illustration with heading "No products yet" and a primary CTA button "Create First Product" linking to Admin Create Product; search bar visible but disabled or cleared.
- Error   → Error banner reading "Products could not be loaded. Please try again." with a retry button; "New Product" button remains active so staff can still navigate to create a product.
- Success → Searchable, paginated table/grid of active (and optionally inactive) products from the products table showing: primary product image thumbnail (from product_images where is_primary = true), product name, brand name (via brand_id), category name (via category_id), base_price_paise formatted as ₹ (tax-inclusive display per BR-7), average_rating, is_active toggle badge; actions per row: Edit (→ Admin Edit Product), and a soft-delete / deactivate toggle; "New Product" button (→ Admin Create Product); role guard — only users with catalogue management permission can see Edit/Create/Deactivate actions (REQ-48, REQ-53, REQ-55).

Admin Create Product:
- Loading → N/A — the form is static; any reference data needed (categories, brands) fetches on mount, showing skeleton selects for those dropdowns only.
- Empty   → N/A — the page is the blank creation form itself; all fields start empty/default.
- Error   → Two error surfaces: (1) if the reference data fetch (categories/brands) fails, inline error messages replace the affected dropdowns with "Could not load options — retry" links; (2) on form submission failure, a banner at the top of the form reads "Product could not be saved." with field-level validation messages (color-error + icon) for required fields (name, base_price_paise, tax_rate_percent) and duplicate slug conflicts.
- Success → Form with fields: Product Name (maps to products.name), Slug (auto-generated, editable), Description (textarea → products.description), Category (select from categories), Brand (select from brands), Base Price in ₹ (converted to paise on save), Tax Rate % (products.tax_rate_percent), Is Returnable toggle, Return Window Days, Is Active toggle, Attributes (JSONB key-value editor for CMS flexibility per REQ-53); Images section for uploading/linking product images (product_images rows, is_primary flag, alt_text, sort_order); SKU section: inline list of SKUs each with Size, Colour, SKU Code, Price in ₹, Stock Quantity fields (maps to skus table, BR-3); "Save Product" primary button and "Cancel" secondary button; on successful save redirects to Admin Edit Product for the new product ID with a success toast.

Admin Edit Product:
- Loading → Skeleton form with placeholder inputs matching the create form layout; product name in breadcrumb shows "…" until data arrives.
- Empty   → N/A — the page only renders for an existing product ID; no data resolves to Error state.
- Error   → If the product cannot be fetched (404 or RBAC denial), full-width error banner "Product not found or you do not have permission to edit it." with a back link to Admin Catalogue Products; if save fails, banner at form top "Changes could not be saved." with field-level error messages.
- Success → Pre-populated form identical in structure to Admin Create Product but loaded with the product's current values from products, product_images, and skus tables; SKU list shows existing SKUs with current stock_quantity and reserved_quantity (read-only reserved field so staff see live stock context); is_active toggle allows deactivation; "Save Changes" primary button; "Delete / Deactivate" destructive action (with confirmation dialog) for authorised roles; on successful save, success toast "Product updated." and stays on the page (REQ-53, REQ-55).

Admin Catalogue Categories:
- Loading → Skeleton table rows (6 placeholder rows) with "New Category" button; page heading "Categories" renders immediately.
- Empty   → Empty-state message "No categories found" with a primary CTA "Create First Category" linking to Admin Create Category.
- Error   → Error banner "Categories could not be loaded. Please try again." with retry button; "New Category" button remains active.
- Success → Table of categories from the categories table showing: name, slug, parent category name (self-join via parent_id for hierarchy display), is_active badge, created_at date; actions per row: Edit (→ Admin Edit Category); "New Category" button (→ Admin Create Category); role-gated create/edit actions (REQ-48, REQ-55).

Admin Create Category:
- Loading → N/A — static form; parent category dropdown fetches on mount, showing a skeleton select only.
- Empty   → N/A — blank creation form by design.
- Error   → On reference data fetch failure, the Parent Category dropdown shows "Could not load — retry"; on submission failure, banner "Category could not be saved." with field-level errors for required fields (name, slug) and duplicate-slug conflict message.
- Success → Form with fields: Category Name (categories.name), Slug (auto-generated, editable, categories.slug), Description (textarea), Parent Category (optional select from existing active categories for hierarchy per categories.parent_id), Is Active toggle; "Save Category" primary button and "Cancel" secondary; on success redirects to Admin Catalogue Categories with success toast.

Admin Edit Category:
- Loading → Skeleton form fields; category name shows "…" in breadcrumb until loaded.
- Empty   → N/A — resolves to Error if ID not found.
- Error   → If category not found or RBAC denied: "Category not found or you do not have permission to edit it." with back link; if save fails: banner "Changes could not be saved." with field-level errors.
- Success → Pre-populated form with current values from categories table (name, slug, description, parent_id resolved to parent name in dropdown, is_active); warning displayed if changing parent_id would create a circular hierarchy; "Save Changes" primary button; on success stays on page with success toast "Category updated." (REQ-55).

Admin Catalogue Brands:
- Loading → Skeleton table rows (6 placeholder rows) with "New Brand" button; page heading "Brands" renders immediately.
- Empty   → Empty-state message "No brands found" with primary CTA "Create First Brand" linking to Admin Create Brand.
- Error   → Error banner "Brands could not be loaded. Please try again." with retry button; "New Brand" button remains active.
- Success → Table of brands from the brands table showing: name, slug, created_at date; actions per row: Edit (→ Admin Edit Brand); "New Brand" button (→ Admin Create Brand); role-gated create/edit actions (REQ-48, REQ-38 filter support).

Admin Create Brand:
- Loading → N/A — fully static form with no external data dependencies.
- Empty   → N/A — blank creation form by design.
- Error   → On submission failure, banner "Brand could not be saved." with field-level errors for required fields (name, slug) and duplicate-slug conflict message (color-error + icon).
- Success → Form with fields: Brand Name (brands.name), Slug (auto-generated from name, editable, brands.slug); "Save Brand" primary button and "Cancel" secondary; on success redirects to Admin Catalogue Brands with success toast "Brand created."

Admin Edit Brand:
- Loading → Skeleton form inputs; brand name shows "…" in breadcrumb until loaded.
- Empty   → N/A — resolves to Error if ID not found.
- Error   → If brand not found or RBAC denied: "Brand not found or you do not have permission to edit it." with back link to Admin Catalogue Brands; if save fails: banner "Changes could not be saved." with field-level errors for name/slug including duplicate-slug conflict.
- Success → Pre-populated form with current values from brands table (name, slug); note showing how many products are linked to this brand (product count via brand_id) to inform the staff member before renaming; "Save Changes" primary button; on success stays on page with success toast "Brand updated." (REQ-55).

Admin Promotions:
- Loading → Skeleton rows replace the promo code table while the GET /admin/promotions request is in flight; the "New Promo Code" button is visible but disabled; a top-of-page indigo progress bar animates.
- Empty   → Table is replaced by a centred illustration and the message "No promo codes yet. Create your first code to offer discounts at checkout." with a prominent "New Promo Code" button; column headers remain visible to establish layout.
- Error   → An inline error banner (color-error, with an alert icon and text label) reads "Failed to load promo codes. Please try again." with a Retry button; the table body is hidden; the "New Promo Code" button remains enabled so staff can still navigate away.
- Success → Full data table listing each promo code with columns: Code, Discount Type (percentage / flat), Discount Value, Minimum Order, Max Uses / Used Count, Expiry Date, Status (Active / Inactive badge with icon), and Actions (Edit link, toggle active/inactive). Pagination or scroll if list is long. Role gate: only users whose role permits promotions management see this page; others are redirected with an "Access denied" message.

Admin Create Promo Code:
- Loading → N/A — this is a static form page with no data fetch required before rendering; the empty form renders immediately.
- Empty   → N/A — the form is the canonical initial state: fields for Code (text), Discount Type (percentage / flat radio), Discount Value (numeric), Minimum Order Value (numeric, optional), Max Uses (numeric, optional), Expiry Date (date picker, optional), and Active toggle, all blank and ready for input.
- Error   → Inline field-level validation messages in color-error (with icons) appear beneath each invalid field (e.g. "Discount value must be greater than 0", "Code already exists"); a toast or banner summarises server-side errors such as duplicate code (unique constraint on promo_codes.code) or unauthorised role; the form remains editable for correction.
- Success → On successful POST, the user is redirected to Admin Promotions with a transient success toast: "Promo code created successfully." No separate success state is shown on this page itself.

Admin Edit Promo Code:
- Loading → Form fields render as disabled skeleton inputs while the GET /admin/promotions/:id request resolves; an indigo progress bar animates at the top.
- Empty   → N/A — if an ID is provided the form either loads data or returns a 404; there is no meaningful empty state distinct from the not-found error.
- Error   → If the promo code ID does not exist, an inline error panel with a warning icon reads "Promo code not found." with a Back to Promotions link. For save failures (validation or server error), field-level color-error messages appear (same pattern as Create); a banner shows server errors such as duplicate code or expired session.
- Success → Form pre-populated with existing values: Code, Discount Type, Discount Value, Minimum Order Value, Max Uses, Used Count (read-only), Expiry Date, Active toggle. On successful PATCH, a success toast reads "Promo code updated." and the user may stay on the page or navigate back; the Used Count field is always read-only to prevent manual tampering.

Admin Returns:
- Loading → Skeleton rows fill the returns queue table while the GET /admin/returns request is in flight; filter controls (status filter: requested / approved / rejected / completed) are visible but inactive.
- Empty   → Table body replaced by centred text and icon: "No return requests found." If a status filter is active, the message reads "No return requests match the current filter." with a Clear Filter link.
- Error   → Inline error banner (color-error, alert icon, text label) reads "Failed to load return requests. Please try again." with a Retry button; table body hidden; filter controls remain accessible.
- Success → Table of return requests with columns: Return ID, Order Number, Customer (name / email), Item (product_name_snapshot), Reason (truncated), Status badge (requested / approved / rejected / completed, each with a distinct icon), Return Window Expires, Requested At, and a View button per row. Status filter chips at top allow narrowing to a single status. Rows with status "requested" are highlighted to draw staff attention. Role gate enforced; unauthorised roles see an access-denied message.

Admin Return Detail:
- Loading → Page chrome (breadcrumb, action buttons) renders immediately; the detail content area shows skeleton blocks for the return metadata panel, item detail card, and action section while GET /admin/returns/:id resolves.
- Empty   → N/A — a detail page for a specific ID either returns data or a not-found error; there is no meaningful empty data state.
- Error   → If the return request ID does not exist, a full-page notice with a warning icon reads "Return request not found." with a Back to Returns link. For action failures (approve / reject submission errors), an inline banner in color-error with an icon reads "Action failed. Please try again." and the action buttons remain enabled.
- Success → Displays: Return Request ID and status badge (with icon); Order Number (linked to order detail); Customer name and email; Item details (product_name_snapshot, variant_snapshot, quantity, unit_price_paise rendered as ₹ value); Return reason (free text from return_requests.reason); Return Window Expires At; Review history (reviewed_by, review_notes, reviewed_at if already actioned). Action section: if status is "requested", shows Approve and Reject buttons (with confirmation prompt); if already approved / rejected / completed, buttons are replaced by a read-only status note. Approving triggers refund recording against the originating order and updates order current_status to return_approved. Role gate: only permitted staff roles see the action buttons.

Admin Reports:
- Loading → Report panels render as skeleton cards with animated placeholder bars representing chart/summary areas while the GET /admin/reports request resolves; a date-range filter control is visible but disabled.
- Empty   → If the selected date range contains no orders, each report panel displays a centred message with an info icon: "No data for the selected period." with a suggestion to widen the date range; the date-range filter remains active.
- Error   → An inline error banner (color-error, alert icon, label) reads "Failed to load report data. Please try again." with a Retry button; report panels are hidden; the date-range filter remains accessible for adjustment.
- Success → Consolidated business report dashboard containing: total orders count, gross revenue (sum of orders.total_paise), total discount applied (sum of orders.discount_paise), total tax collected (sum of orders.tax_paise), total shipping charges, order status breakdown (confirmed / packed / shipped / delivered / cancelled counts), return requests summary (requested / approved / rejected counts), and promo code usage summary (promo_codes.code with used_count). All monetary values displayed in ₹. Date-range filter allows narrowing the reporting window. Role gate: only roles with reporting access can view this page; others see an access-denied message.

Admin User List:
- Loading → Skeleton rows fill the users table while the GET /admin/users request resolves; search input and role-filter dropdown are visible but inactive; an indigo progress bar animates.
- Empty   → Table body replaced by centred icon and text: "No users found." If a search or filter is active: "No users match your search. Try adjusting the filters." with a Clear Filters link.
- Error   → Inline error banner (color-error, alert icon, label) reads "Failed to load users. Please try again." with a Retry button; table body hidden; search and filter controls remain functional.
- Success → Paginated table of user accounts with columns: Full Name, Email, Phone, Roles (badges per role name from the roles table), Account Status (Active / Inactive badge with icon derived from users.is_active), Guest Converted (boolean flag from users.is_guest_converted), Registered At (users.created_at), and a View link per row. Search input filters by name or email. Role filter dropdown narrows list by assigned role. Role gate: only admin-level roles can access this page.

Admin User Detail:
- Loading → Page chrome (breadcrumb, role assignment panel header) renders immediately; the user profile card and roles section show skeleton blocks while GET /admin/users/:id resolves.
- Empty   → N/A — a detail page for a specific user ID either returns data or a not-found error; no meaningful empty data state exists.
- Error   → If the user ID does not exist, a full-page notice with a warning icon reads "User not found." with a Back to Users link. For save failures (role assignment update errors), an inline color-error banner with icon and label reads "Failed to update user. Please try again." and the form remains editable.
- Success → Displays: user profile card (Full Name, Email, Phone, Status badge, Guest Converted flag, Created At, Updated At); Current Roles section listing all assigned roles (from user_roles joined to roles) as labelled badges; Role Management section with a multi-select or checklist to add or remove roles (constrained to roles defined in the roles table); a Save Changes button. Destructive actions (deactivating an account via is_active) are presented with a confirmation prompt and a warning icon. Changes are submitted via PATCH; on success a toast reads "User updated successfully." Role gate: only admin-level roles can assign or revoke roles or deactivate accounts.

Not Found:
- Loading → N/A — this is a static client-side catch-all route rendered immediately with no data fetch.
- Empty   → N/A — the page content is always present by definition; there is no data-driven empty state.
- Error   → N/A — the page itself is the error response for an unmatched route; no secondary error state applies.
- Success → Centred layout on color-canvas background: a large "404" in type-display weight, a heading "Page not found" in type-h1, a body message "The page you're looking for doesn't exist or has been moved." in type-body / color-body, and a primary "Go to Home" button (color-primary) plus a secondary "Go Back" link. No data fetch is involved; the page renders instantly.

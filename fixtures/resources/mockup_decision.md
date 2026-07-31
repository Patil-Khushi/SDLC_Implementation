# HTML Mockup — Design Direction

**App title:** ShopFlow

## Layout direction
A clean e-commerce storefront with a sticky top navbar (logo left, search centre, cart/account icons right) for all customer-facing pages, and a collapsible left sidebar navigation for all admin pages. Customer screens follow a generous single-column or grid rhythm with clear section breaks; admin screens use a dense data-table layout with action toolbars. The overall feel is modern and minimal — white backgrounds, subtle card shadows, and a consistent primary accent colour throughout both areas.

## Navigation groups
### Storefront
- Home
- Product Listing
- Product Listing by Category
- Search Results
- Product Detail

### Cart & Checkout
- Cart
- Checkout - Address
- Checkout - Payment
- Checkout - Review
- Checkout - Confirmation
- Guest Post-Checkout Register

### Auth
- Login
- Register
- Forgot Password
- Reset Password

### My Account
- Account Overview
- Account Profile
- Account Addresses
- Account Add Address
- Account Edit Address
- Order History
- Order Detail
- Return Request
- Notifications

### Admin
- Admin Dashboard
- Admin Order List
- Admin Order Detail
- Admin Catalogue Products
- Admin Create Product
- Admin Edit Product
- Admin Catalogue Categories
- Admin Create Category
- Admin Edit Category
- Admin Catalogue Brands
- Admin Create Brand
- Admin Edit Brand
- Admin Promotions
- Admin Create Promo Code
- Admin Edit Promo Code
- Admin Returns
- Admin Return Detail
- Admin Reports
- Admin User List
- Admin User Detail

### System
- Not Found

## Screens rendered
- Home  (`/`)
- Product Listing  (`/products`)
- Product Listing by Category  (`/categories/:slug/products`)
- Search Results  (`/search`)
- Product Detail  (`/products/:slug`)
- Cart  (`/cart`)
- Checkout - Address  (`/checkout/address`)
- Checkout - Payment  (`/checkout/payment`)
- Checkout - Review  (`/checkout/review`)
- Checkout - Confirmation  (`/checkout/confirmation`)
- Guest Post-Checkout Register  (`/checkout/register`)
- Login  (`/login`)
- Register  (`/register`)
- Forgot Password  (`/forgot-password`)
- Reset Password  (`/reset-password`)
- Account Overview  (`/account`)
- Account Profile  (`/account/profile`)
- Account Addresses  (`/account/addresses`)
- Account Add Address  (`/account/addresses/new`)
- Account Edit Address  (`/account/addresses/:id/edit`)
- Order History  (`/account/orders`)
- Order Detail  (`/account/orders/:id`)
- Return Request  (`/account/orders/:id/return`)
- Notifications  (`/account/notifications`)
- Admin Dashboard  (`/admin`)
- Admin Order List  (`/admin/orders`)
- Admin Order Detail  (`/admin/orders/:id`)
- Admin Catalogue Products  (`/admin/catalogue/products`)
- Admin Create Product  (`/admin/catalogue/products/new`)
- Admin Edit Product  (`/admin/catalogue/products/:id/edit`)
- Admin Catalogue Categories  (`/admin/catalogue/categories`)
- Admin Create Category  (`/admin/catalogue/categories/new`)
- Admin Edit Category  (`/admin/catalogue/categories/:id/edit`)
- Admin Catalogue Brands  (`/admin/catalogue/brands`)
- Admin Create Brand  (`/admin/catalogue/brands/new`)
- Admin Edit Brand  (`/admin/catalogue/brands/:id/edit`)
- Admin Promotions  (`/admin/promotions`)
- Admin Create Promo Code  (`/admin/promotions/new`)
- Admin Edit Promo Code  (`/admin/promotions/:id/edit`)
- Admin Returns  (`/admin/returns`)
- Admin Return Detail  (`/admin/returns/:id`)
- Admin Reports  (`/admin/reports`)
- Admin User List  (`/admin/users`)
- Admin User Detail  (`/admin/users/:id`)
- Not Found  (`*`)

## Deliverable coverage
- **C1 Screens** — one functional section per route (above).
- **C2 Design System** — style-guide gallery renders every component with all variants/states.
- **C3 Responsive** — media queries at <=375 / 376-768 / >=1024; product grid 1/2/4 cols; sidebar collapses to a hamburger.
- **C5 Interactions** — hover darken 8%, click scale 0.98, loading spinner+disabled, success toast (auto-dismiss 3s), modal fade+slide-up 200ms.

_Assets: none found — mockup uses placeholders._
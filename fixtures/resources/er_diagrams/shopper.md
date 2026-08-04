```mermaid
erDiagram
    users {
        uuid id PK
        varchar email
        varchar full_name
        boolean is_active
    }

    addresses {
        uuid id PK
        uuid user_id FK
        varchar full_name
        varchar city
        boolean is_default
    }

    products {
        uuid id PK
        uuid category_id FK
        uuid brand_id FK
        varchar name
        bigint base_price_paise
        boolean is_active
    }

    skus {
        uuid id PK
        uuid product_id FK
        varchar sku_code
        varchar size
        bigint price_paise
    }

    carts {
        uuid id PK
        uuid user_id FK
        uuid promo_code_id FK
        varchar status
        varchar guest_email
    }

    orders {
        uuid id PK
        uuid user_id FK
        uuid cart_id FK
        varchar order_number
        varchar current_status
        bigint total_paise
    }

    order_items {
        uuid id PK
        uuid order_id FK
        uuid sku_id FK
        varchar product_name_snapshot
        integer quantity
        bigint unit_price_paise
    }

    users ||--o{ addresses : "saves"
    users ||--o{ carts : "owns"
    users ||--o{ orders : "places"
    carts ||--o{ order_items : "becomes"
    skus ||--o{ order_items : "purchased as"
    products ||--o{ skus : "has"
    orders ||--o{ order_items : "contains"
    carts }o--|| orders : "checked out into"
```

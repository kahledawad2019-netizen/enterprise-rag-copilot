# Business glossary

Replace the examples below with the real meaning of your data. This file is
loaded into the vector store by `python -m src.train` and is what teaches the
model the things the schema alone cannot express.

## Definitions
- **Active customer**: a customer with at least one order in the last 12 months.
- **Net revenue**: `list_price * quantity * (1 - discount)`, discounts included.

## Conventions
- Money columns are stored in USD with 2 decimal places.
- Date columns are `date`, not `datetime`; there is no time component.
- Soft-deleted rows are marked, not removed - exclude them unless asked.

## Query hints
- "This year" means the current calendar year: `YEAR(order_date) = YEAR(GETDATE())`.
- Prefer `sales.orders` over any reporting view when counting orders.

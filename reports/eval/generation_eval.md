## generation evaluation

| Metric | Value |
|---|---|
| `total` | 26 |
| `passed` | 18 |
| `failed` | 8 |
| `accuracy` | 0.6923 |
| `execution_accuracy` | 0.6667 |
| `ex_scored_cases` | 24 |
| `golden_missing_cases` | 0 |
| `valid_sql_cases` | 0 |
| `valid_rate` | None |
| `skip_cases` | 2 |
| `avg_repairs` | 0 |
| `repaired_saved` | 0 |

### Reported counts

### Failures (8)
| Case | Question | Detail |
|---|---|---|
| `gen_rev_by_state` | Total revenue by customer state | 3+ row pairs differ beyond tolerance (rel_tol=0.0001, abs_tol=0.01). |
| `gen_delivered_orders` | Số đơn giao thành công là bao nhiêu? | Row count mismatch: expected 1, got 100. |
| `gen_delivered_orders` | How many orders were delivered successfully? | Column arity mismatch: expected 1, got 3. |
| `gen_freight_by_category` | Phí vận chuyển trung bình theo danh mục sản phẩm? | Column arity mismatch: expected 2, got 3. |
| `gen_freight_by_category` | Average shipping cost per product category | Column arity mismatch: expected 2, got 3. |
| `gen_top5_categories_gross` | Top 5 danh mục sản phẩm có doanh thu gộp cao nhất? | 3+ row pairs differ beyond tolerance (rel_tol=0.0001, abs_tol=0.01). |
| `gen_canceled_by_state` | Number of canceled orders by customer state | Column arity mismatch: expected 2, got 3. |
| `gen_customer_count` | Tổng số khách hàng là bao nhiêu? | Row count mismatch: expected 1, got 27. |

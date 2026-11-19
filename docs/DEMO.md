# Chatbot demo: one model, three setups

An informal hands-on test run on 2026-11-20 with a real chatbot: the Claude desktop app's
chat, model Claude Opus 5.5. The same 9 questions about a synthetic shop database were
asked three ways. It was one run per setup, not a benchmark. For measured numbers, see the
BIRD results in the README.

## The database

`python benchmarks/trap_shop.py` rebuilds it exactly (fixed seed, SQLite, about 14 MB): 5,000
customers, 29 products, 50,000 orders (Jan 2025 – Oct 2026), 85,646 order items, 60,507
payment rows, 3,082 refunds and an `fx_rates` table. All the data is invented. It contains
ten traps that make SQL silently wrong:

1. `orders.status` is upper case (`'CANCELLED'`); there is no `'completed'`.
2. `customers.country` holds ISO codes (`'GB'`, not `'UK'`).
3. `products.category` holds short codes (`'ELEC'`, not `'Electronics'`).
4. `orders.channel` uses `'-'` for unknown, not NULL.
5. `refunds.refund_date` is text in `DD/MM/YYYY`.
6. `orders.discount_amount` is NULL when there was no discount.
7. Orders are in four currencies (INR, USD, GBP, EUR).
8. `payments` has one row per attempt (failed, then captured).
9. Deleted and test accounts (`is_deleted = 1`) still have orders.
10. Some people have two accounts (same email, different case).

## The three setups

The tools were connected to the chat as MCP servers on the same machine.

| Round | Tools | Instructions to the model |
|---|---|---|
| **A** | goldcheck | Write one query per question from your best guess, run it with `check_sql`, answer from what it returns. Don't look at the data first and don't rewrite. |
| **B** | goldcheck | Look at the data with `describe_values` first, run every query through `check_sql`, fix any error, answer only from a query that passes. |
| **C** | a plain `run_sql` tool (read-only, returns rows, no checks) | Same careful instructions as B: explore the tables and real values first, check results make sense, answer only from a query you trust. |

Each round was a new chat.

## Results

| # | Question | Expected | A | B | C |
|---|---|---|---|---|---|
| 1 | Orders cancelled in Q3 2026 | 1,178 | 0, flagged ✗ | 1,178 ✓ | 1,178 ✓ |
| 2 | Customers in the UK | 727 (765 incl. deleted) | 0, flagged ✗ | 727 ✓ | 727 ✓ |
| 3 | Top 5 Electronics products by units, 2026 | Earbuds, Smart Watch, USB-C Charger, Power Bank, Webcam | empty, flagged ✗ | same 5 ✓ | same 5 ✓ (also excluded returns, stated) |
| 4 | Refunds for late delivery | 600 | 0, flagged ✗ | 600 ✓ | 600 ✓ |
| 5 | Orders paid with UPI | 8,849 | query error (column doesn't exist) ✗ | 8,849 ✓ | 8,849 ✓ |
| 6 | Refunds in October 2026, USD | $34,736.69 | NULL, flagged ✗ | $34,736.69 ✓ | $34,736.69 ✓ |
| 7 | Net revenue October 2026, USD | $702,794.18 * | $29.5M, flagged ✗ | $747,435.49 ~ | $700,056.58 ~ |
| 8 | Orders from an unknown channel | 644 | 0, **not flagged** ✗ | 644 ✓ | 644 ✓ |
| 9 | Unique customers who ordered in 2026 | 4,953 accounts / 4,544 active people * | 4,953, not flagged ~ | 4,771 ~ | 4,771 ~ (also gave 4,544) |

✓ correct · ~ a defensible answer under a definition the bot stated · ✗ wrong · "flagged" =
goldcheck reported an error, and the bot said the number was wrong instead of presenting it.

\* Questions 7 and 9 depend on definitions. The expected value for 7 counts delivered and
shipped orders, total minus discount, converted to USD. B also subtracted refunds issued
in October; C subtracted refunds on those orders. For 9, B and C counted people by email,
including deleted accounts.

**Summary**
- **A:** no usable answers, but 7 of 9 reported as wrong rather than given as fact. The two
  not flagged (8 and 9) are outside what `check_sql` checks.
- **B:** 7 of 9 matched exactly; 2 used a stated definition.
- **C:** the same as B, and slightly more thorough (it also noticed unrequested things, such
  as channel tracking only starting in April 2025).

**Rewrite check (round B setup):** asked whether
`SUM(total_amount) … WHERE status = 'DELIVERED' AND currency = 'INR'` could safely be
rewritten with a join to `payments`, `compare_queries` reported different results
(₹148,974,325.45 vs ₹182,422,565.54, +22.5%). It returned a 1-row counterexample and blamed
the `FROM`/`JOIN` clause. The model then explained the cause: payment retries duplicate
orders in the join.

## What it shows

- A strong model told to explore first found every trap with a plain SQL tool. goldcheck
  didn't improve its accuracy.
- Exploring first took many tool calls per question. Told to answer in one go, the same model
  produced no usable numbers. goldcheck turned 7 of those into "this is wrong" instead of a
  confident `0`.

## Limits

One run per setup, one model, 9 questions. The app's memory feature was on, so rounds B and
C may have benefited from earlier chats. Time and number of tool calls weren't recorded.

"""A fictional shop database, twelve questions with gold SQL, and a deliberately flawed agent."""
from __future__ import annotations

import json
import os
import random
import sqlite3

SCHEMA = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT, segment TEXT);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, category TEXT, price REAL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, order_date TEXT, status TEXT);
CREATE TABLE order_items (order_id INTEGER, product_id INTEGER, qty INTEGER, unit_price REAL);
CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER, amount REAL);
"""
CITIES = ["Northwick", "Eastbay", "Harlow Falls", "Pinecrest"]
SEGMENTS = ["Consumer", "Business"]
CATEGORIES = ["Tea", "Coffee", "Mugs", "Gifts"]
REVENUE = "SUM(oi.qty * oi.unit_price)"

QUESTIONS = [
    ("q01", "How many customers do we have?", "counting", "SELECT COUNT(*) FROM customers"),
    ("q02", "How many orders were completed?", "filters", "SELECT COUNT(*) FROM orders WHERE status = 'completed'"),
    ("q03", "What is total revenue from completed orders?", "revenue",
     f"SELECT {REVENUE} FROM order_items oi JOIN orders o ON o.id = oi.order_id WHERE o.status = 'completed'"),
    ("q04", "How many customers have placed at least one order?", "counting",
     "SELECT COUNT(DISTINCT customer_id) FROM orders"),
    ("q05", "What is completed revenue by product category?", "revenue",
     f"SELECT p.category, {REVENUE} FROM order_items oi JOIN orders o ON o.id = oi.order_id "
     "JOIN products p ON p.id = oi.product_id WHERE o.status = 'completed' GROUP BY p.category"),
    ("q06", "How many customers are in the Business segment?", "filters",
     "SELECT COUNT(*) FROM customers WHERE segment = 'Business'"),
    ("q07", "What is the total amount refunded?", "revenue", "SELECT SUM(amount) FROM refunds"),
    ("q08", "What is completed revenue per city?", "joins",
     f"SELECT c.city, {REVENUE} FROM order_items oi JOIN orders o ON o.id = oi.order_id "
     "JOIN customers c ON c.id = o.customer_id WHERE o.status = 'completed' GROUP BY c.city"),
    ("q09", "Which three products sold the most units in completed orders, best first?", "ranking",
     "SELECT p.name, SUM(oi.qty) AS units FROM order_items oi JOIN orders o ON o.id = oi.order_id "
     "JOIN products p ON p.id = oi.product_id WHERE o.status = 'completed' "
     "GROUP BY p.id ORDER BY units DESC, p.name LIMIT 3"),
    ("q10", "How many orders were placed in March 2026?", "dates",
     "SELECT COUNT(*) FROM orders WHERE order_date >= '2026-03-01' AND order_date < '2026-04-01'"),
    ("q11", "What is the average value of a completed order?", "revenue",
     "SELECT AVG(t) FROM (SELECT SUM(oi.qty * oi.unit_price) AS t FROM order_items oi "
     "JOIN orders o ON o.id = oi.order_id WHERE o.status = 'completed' GROUP BY o.id)"),
    ("q12", "How many products have never been ordered?", "joins",
     "SELECT COUNT(*) FROM products WHERE id NOT IN (SELECT product_id FROM order_items)"),
]
ORDERED = {"q09"}

# What a plausible but careless agent might return. Seven right, five wrong in different ways.
FLAWED = {
    "q01": "SELECT COUNT(id) AS customers FROM customers",
    "q02": "SELECT COUNT(*) FROM orders WHERE status = 'Completed'",  # wrong literal case -> 0
    "q03": "SELECT SUM(oi.qty * oi.unit_price) FROM order_items oi JOIN orders o ON o.id = oi.order_id",  # no status filter
    "q04": "SELECT COUNT(customer_id) FROM orders",  # counts orders, not customers
    "q05": f"SELECT {REVENUE} AS revenue, p.category FROM order_items oi JOIN orders o ON o.id = oi.order_id "
           "JOIN products p ON p.id = oi.product_id WHERE o.status = 'completed' GROUP BY p.category",
    "q06": "SELECT COUNT(*) FROM customers WHERE segment = 'Business'",
    "q07": "SELECT SUM(refund_amount) FROM refunds",  # column does not exist
    "q08": f"SELECT c.city, {REVENUE} FROM order_items oi JOIN orders o ON o.id = oi.order_id "
           "JOIN customers c ON c.id = o.customer_id JOIN refunds r ON r.order_id = o.id "
           "WHERE o.status = 'completed' GROUP BY c.city",  # inner join to refunds drops orders
    "q09": "SELECT p.name, SUM(oi.qty) AS units FROM order_items oi JOIN orders o ON o.id = oi.order_id "
           "JOIN products p ON p.id = oi.product_id WHERE o.status = 'completed' "
           "GROUP BY p.id ORDER BY units DESC, p.name LIMIT 3",
    "q10": "SELECT COUNT(*) FROM orders WHERE strftime('%Y-%m', order_date) = '2026-03'",
    "q11": "SELECT SUM(oi.qty * oi.unit_price) * 1.0 / COUNT(DISTINCT o.id) FROM order_items oi "
           "JOIN orders o ON o.id = oi.order_id WHERE o.status = 'completed'",
    "q12": "SELECT COUNT(*) FROM products p LEFT JOIN order_items oi ON oi.product_id = p.id WHERE oi.product_id IS NULL",
}


# A ready-made pair for `goldcheck compare`: a "refactor" that reorders the joins (harmless)
# and also counts pending orders (the bug).
COMPARE_OLD = """SELECT c.city, SUM(oi.qty * oi.unit_price) AS revenue
FROM order_items oi
JOIN orders o ON o.id = oi.order_id
JOIN customers c ON c.id = o.customer_id
WHERE o.status = 'completed'
GROUP BY c.city
ORDER BY c.city"""
COMPARE_NEW = """SELECT c.city, SUM(oi.qty * oi.unit_price) AS revenue
FROM customers c
JOIN orders o ON o.customer_id = c.id
JOIN order_items oi ON oi.order_id = o.id
WHERE o.status <> 'cancelled'
GROUP BY c.city
ORDER BY c.city"""


def build(out_dir: str):
    """Write demo.sqlite, questions.json and flawed_agent.jsonl into out_dir. Fully deterministic."""
    os.makedirs(out_dir, exist_ok=True)
    db = os.path.join(out_dir, "demo.sqlite")
    if os.path.exists(db):
        os.remove(db)
    rng = random.Random(7)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO customers VALUES (?,?,?,?)",
                    [(i, f"Customer {i:03d}", rng.choice(CITIES), rng.choice(SEGMENTS)) for i in range(1, 61)])
    con.executemany("INSERT INTO products VALUES (?,?,?,?)",
                    [(i, f"{CATEGORIES[i % 4]} item {i:02d}", CATEGORIES[i % 4], round(rng.uniform(3, 40), 2))
                     for i in range(1, 25)])
    prices = dict(con.execute("SELECT id, price FROM products"))
    for oid in range(1, 301):
        status = rng.choices(["completed", "cancelled", "pending"], [0.75, 0.15, 0.10])[0]
        day = f"2026-{rng.randint(1, 6):02d}-{rng.randint(1, 28):02d}"
        con.execute("INSERT INTO orders VALUES (?,?,?,?)", (oid, rng.randint(1, 50), day, status))
        for pid in rng.sample(range(1, 21), rng.randint(1, 3)):  # customers 51-60 and products 21-24 stay unused
            con.execute("INSERT INTO order_items VALUES (?,?,?,?)", (oid, pid, rng.randint(1, 4), prices[pid]))
        if status == "completed" and rng.random() < 0.12:
            con.execute("INSERT INTO refunds (order_id, amount) VALUES (?,?)", (oid, round(rng.uniform(2, 20), 2)))
    con.commit()
    con.close()
    questions = [{"id": i, "question": q, "category": c, "gold_sql": s, "ordered": i in ORDERED}
                 for i, q, c, s in QUESTIONS]
    qpath = os.path.join(out_dir, "questions.json")
    with open(qpath, "w", encoding="utf-8") as fh:
        json.dump(questions, fh, indent=2)
    for name, sql in (("old_query.sql", COMPARE_OLD), ("new_query.sql", COMPARE_NEW)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(sql + "\n")
    apath = os.path.join(out_dir, "flawed_agent.jsonl")
    with open(apath, "w", encoding="utf-8") as fh:
        for qid, sql in FLAWED.items():
            fh.write(json.dumps({"id": qid, "sql": sql}) + "\n")
    return db, qpath, apath

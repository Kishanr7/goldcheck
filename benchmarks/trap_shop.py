"""Build the synthetic shop database used in docs/DEMO.md.

Everything is invented and generated from a fixed seed, so every run gives the same
database. Usage:  python benchmarks/trap_shop.py [OUTPUT.sqlite]   (default: trap_shop.sqlite)

The traps (what makes a chatbot's SQL silently wrong):
  1. orders.status is UPPERCASE ('DELIVERED', 'CANCELLED', ...); there is no 'completed'.
  2. customers.country holds ISO codes: 'IN', 'GB' (not 'India', not 'UK').
  3. products.category holds short codes: 'ELEC', 'HOME', 'TOYS', 'BOOK', 'BEAU'.
  4. orders.channel uses '-' for "unknown" (legacy rows), not NULL.
  5. refunds.refund_date is text in DD/MM/YYYY, not YYYY-MM-DD.
  6. orders.discount_amount is NULL when there was no discount (total - discount -> NULL).
  7. orders are in four currencies; summing total_amount across them is meaningless.
  8. payments has one row per attempt (failed, then captured): joining it duplicates orders.
  9. customers.is_deleted = 1 marks test and closed accounts, which still have orders.
 10. some people signed up twice (same email, different case): customers != people.
"""
import os
import random
import sqlite3
from datetime import date, datetime, timedelta

import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "trap_shop.sqlite"
R = random.Random(20261119)

START, END = date(2025, 1, 1), date(2026, 10, 31)
COUNTRIES = [("IN", "INR", 0.40, ["Mumbai", "Bengaluru", "Delhi", "Pune", "Chennai"]),
             ("US", "USD", 0.25, ["Austin", "Seattle", "Chicago", "Denver"]),
             ("GB", "GBP", 0.15, ["London", "Leeds", "Bristol"]),
             ("DE", "EUR", 0.10, ["Berlin", "Munich", "Hamburg"]),
             ("AE", "USD", 0.05, ["Dubai", "Abu Dhabi"]),
             ("SG", "USD", 0.05, ["Singapore"])]
TO_USD = {"USD": 1.0, "INR": 0.012, "GBP": 1.27, "EUR": 1.08}
FIRST = ["Aarav", "Diya", "Rohan", "Isha", "Kabir", "Meera", "Arjun", "Anaya", "Vikram", "Sara", "James", "Emma",
         "Liam", "Olivia", "Noah", "Ava", "Lukas", "Mia", "Omar", "Layla", "Wei", "Mei", "Ethan", "Chloe"]
LAST = ["Sharma", "Patel", "Iyer", "Reddy", "Khan", "Gupta", "Smith", "Brown", "Jones", "Taylor", "Muller",
        "Schmidt", "Ali", "Hassan", "Tan", "Lim", "Wilson", "Clark", "Nair", "Das"]
CATEGORIES = {"ELEC": ["Wireless Earbuds", "USB-C Charger", "Smart Watch", "Bluetooth Speaker", "Power Bank",
                       "Laptop Stand", "Webcam", "Mechanical Keyboard"],
              "HOME": ["Ceramic Mug Set", "Bedsheet", "Desk Lamp", "Storage Box", "Wall Clock", "Cushion Cover"],
              "TOYS": ["Building Blocks", "Puzzle 1000pc", "RC Car", "Board Game", "Plush Bear"],
              "BOOK": ["Data Science Handbook", "Mystery Novel", "Cookbook", "Kids Atlas", "Poetry Collection"],
              "BEAU": ["Face Wash", "Sunscreen SPF50", "Hair Oil", "Lip Balm", "Perfume"]}
PRICE = {"ELEC": (15, 180), "HOME": (8, 60), "TOYS": (10, 70), "BOOK": (6, 35), "BEAU": (4, 40)}


def rand_day(lo=START, hi=END):
    return lo + timedelta(days=R.randrange((hi - lo).days + 1))


def build():
    if os.path.exists(OUT):
        sys.exit(f"{OUT} already exists; choose another path or delete it first")
    con = sqlite3.connect(OUT)
    con.executescript("""
    CREATE TABLE customers (
        customer_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, email TEXT NOT NULL,
        country TEXT NOT NULL, city TEXT, segment TEXT NOT NULL, signup_date TEXT NOT NULL,
        is_deleted INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE products (
        product_id INTEGER PRIMARY KEY, sku TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
        list_price_usd REAL NOT NULL, is_active INTEGER NOT NULL);
    CREATE TABLE orders (
        order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
        order_date TEXT NOT NULL, status TEXT NOT NULL, channel TEXT NOT NULL, currency TEXT NOT NULL,
        total_amount REAL NOT NULL, discount_amount REAL, coupon_code TEXT);
    CREATE TABLE order_items (
        order_id INTEGER NOT NULL REFERENCES orders(order_id), line_no INTEGER NOT NULL,
        product_id INTEGER NOT NULL REFERENCES products(product_id), quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL, PRIMARY KEY (order_id, line_no));
    CREATE TABLE payments (
        payment_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(order_id),
        attempted_at TEXT NOT NULL, method TEXT NOT NULL, status TEXT NOT NULL, amount REAL NOT NULL);
    CREATE TABLE refunds (
        refund_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(order_id),
        refund_date TEXT NOT NULL, amount REAL NOT NULL, reason TEXT NOT NULL);
    CREATE TABLE fx_rates (currency TEXT PRIMARY KEY, usd_per_unit REAL NOT NULL);
    """)

    con.executemany("INSERT INTO fx_rates VALUES (?, ?)", sorted(TO_USD.items()))

    # products
    products = []
    pid = 0
    for cat, names in CATEGORIES.items():
        for n in names:
            pid += 1
            lo, hi = PRICE[cat]
            products.append((pid, f"{cat}-{pid:04d}", n, cat, round(R.uniform(lo, hi), 2), 0 if R.random() < 0.1 else 1))
    con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", products)

    # customers, with duplicate sign-ups and deleted/test accounts
    customers = []
    for cid in range(1, 5001):
        country, cur, _, cities = R.choices(COUNTRIES, weights=[c[2] for c in COUNTRIES])[0]
        first, last = R.choice(FIRST), R.choice(LAST)
        email = f"{first}.{last}{R.randrange(1, 999)}@example.com".lower()
        seg = R.choices(["consumer", "smb", "enterprise"], weights=[80, 15, 5])[0]
        customers.append([cid, f"{first} {last}", email, country, R.choice(cities), seg,
                          rand_day(date(2024, 6, 1), END).isoformat(), 0])
    for c in R.sample(customers, 150):                       # trap 10: same person, second account
        twin = customers[R.randrange(len(customers))]
        c[1], c[2], c[3], c[4] = twin[1], twin[2].capitalize(), twin[3], twin[4]
    for c in R.sample(customers, 200):                       # trap 9: deleted / test accounts
        c[7] = 1
    for i in range(1, 41):
        customers[-i][1], customers[-i][2], customers[-i][7] = f"Test User {i}", f"qa{i}@test.internal", 1
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)", customers)
    by_id = {c[0]: c for c in customers}
    currency_of = {code: cur for code, cur, _, _ in COUNTRIES}

    # orders, items, payments, refunds
    orders, items, payments, refunds = [], [], [], []
    pay_id = ref_id = 0
    weights = [1.0 + (i % 7 == 0) * 2 for i in range(len(customers))]     # some loyal heavy buyers
    for oid in range(1, 50001):
        cust = R.choices(customers, weights=weights)[0]
        lo = max(START, date.fromisoformat(cust[6]))
        d = rand_day(lo, END) if lo <= END else END
        ts = datetime(d.year, d.month, d.day, R.randrange(24), R.randrange(60), R.randrange(60))
        age = (END - d).days
        if age < 3:
            status = R.choice(["PENDING", "SHIPPED"])
        elif age < 10:
            status = R.choices(["SHIPPED", "DELIVERED", "CANCELLED"], weights=[50, 40, 10])[0]
        else:
            status = R.choices(["DELIVERED", "CANCELLED", "RETURNED"], weights=[86, 9, 5])[0]
        channel = "-" if (d < date(2025, 4, 1) and R.random() < 0.35) else R.choices(["web", "app", "store"], weights=[50, 40, 10])[0]
        cur = currency_of[cust[3]]
        rate = TO_USD[cur]
        total = 0.0
        for line in range(1, R.choices([1, 2, 3, 4], weights=[55, 25, 13, 7])[0] + 1):
            p = R.choice(products)
            qty = R.choices([1, 2, 3], weights=[80, 15, 5])[0]
            unit = round(p[4] / rate, 2)
            items.append((oid, line, p[0], qty, unit))
            total += qty * unit
        total = round(total, 2)
        discount, coupon = None, None                          # trap 6: NULL, not 0
        if R.random() < 0.22:
            coupon = R.choice(["WELCOME10", "FESTIVE15", "FLASH20"])
            discount = round(total * int(coupon[-2:]) / 100, 2)
        orders.append((oid, cust[0], ts.isoformat(sep=" "), status, channel, cur, total, discount, coupon))

        method = R.choices(["card", "upi", "netbanking", "wallet", "cod"] if cur == "INR" else ["card", "wallet"],
                           weights=[30, 45, 10, 10, 5] if cur == "INR" else [85, 15])[0]
        paid = round(total - (discount or 0), 2)
        if status != "PENDING":
            for _ in range(R.choices([0, 1, 2], weights=[82, 14, 4])[0]):      # trap 8: failed attempts first
                pay_id += 1
                payments.append((pay_id, oid, (ts + timedelta(seconds=R.randrange(5, 90))).isoformat(sep=" "),
                                 method, "failed", paid))
            pay_id += 1
            payments.append((pay_id, oid, (ts + timedelta(minutes=R.randrange(2, 30))).isoformat(sep=" "),
                             method, "captured", paid))
        if status == "RETURNED" or (status == "DELIVERED" and R.random() < 0.02):
            rd = d + timedelta(days=R.randrange(3, 25))
            if rd <= END:
                ref_id += 1
                amt = paid if status == "RETURNED" else round(paid * R.choice([0.3, 0.5]), 2)
                reason = R.choices(["DAMAGED", "LATE_DELIVERY", "WRONG_ITEM", "CHANGED_MIND"], weights=[30, 20, 20, 30])[0]
                refunds.append((ref_id, oid, rd.strftime("%d/%m/%Y"), amt, reason))   # trap 5
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", orders)
    con.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
    con.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?)", payments)
    con.executemany("INSERT INTO refunds VALUES (?,?,?,?,?)", refunds)
    con.executescript("""
    CREATE INDEX ix_orders_customer ON orders(customer_id);
    CREATE INDEX ix_orders_date ON orders(order_date);
    CREATE INDEX ix_items_product ON order_items(product_id);
    CREATE INDEX ix_payments_order ON payments(order_id);
    CREATE INDEX ix_refunds_order ON refunds(order_id);
    """)
    con.commit()
    con.close()
    print(f"wrote {OUT}: {len(customers)} customers, {len(products)} products, {len(orders)} orders, "
          f"{len(items)} order items, {len(payments)} payments, {len(refunds)} refunds")


if __name__ == "__main__":
    build()

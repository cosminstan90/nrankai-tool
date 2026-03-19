import sqlite3

conn = sqlite3.connect('api/data/analyzer.db')
cur = conn.cursor()

print("--- Error Message ---")
cur.execute("SELECT error_message FROM audits WHERE id='d127e706-bc6a-487d-94c8-e968e391b496'")
print(cur.fetchone()[0])

print("\n--- Audit Logs ---")
cur.execute("SELECT message FROM audit_logs WHERE audit_id='d127e706-bc6a-487d-94c8-e968e391b496' ORDER BY created_at DESC LIMIT 10")
for row in cur.fetchall():
    print(row[0])

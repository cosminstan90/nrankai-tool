"""Test script to diagnose bulk insert issues with SQLAlchemy async."""
import asyncio
import sys
sys.path.insert(0, ".")

from api.models.database import AsyncSessionLocal, GscPageRow, GscQueryRow
from sqlalchemy import delete as sql_delete, insert as sql_insert, select, func, text

PROPERTY_ID = "c0d414da-9ef1-4149-8227-852ea87adcd2"

async def main():
    # 1. Count current rows
    async with AsyncSessionLocal() as db:
        q = (await db.execute(select(func.count()).where(GscQueryRow.property_id == PROPERTY_ID))).scalar()
        p = (await db.execute(select(func.count()).where(GscPageRow.property_id == PROPERTY_ID))).scalar()
        print(f"Before: {q} queries, {p} pages")

    # 2. Delete all
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(GscQueryRow).where(GscQueryRow.property_id == PROPERTY_ID))
        await db.execute(sql_delete(GscPageRow).where(GscPageRow.property_id == PROPERTY_ID))
        await db.commit()
    print("Deleted old rows")

    # 3. Insert 10 test page rows
    test_pages = [
        {"property_id": PROPERTY_ID, "page": f"https://test.com/page-{i}",
         "clicks": i, "impressions": i*10, "ctr": 0.05, "position": float(i)}
        for i in range(10)
    ]
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(sql_insert(GscPageRow), test_pages)
            await db.commit()
            print("Page insert: committed")
        except Exception as e:
            print(f"Page insert FAILED: {e}")
            import traceback; traceback.print_exc()

    # 4. Check result
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(func.count()).where(GscPageRow.property_id == PROPERTY_ID))).scalar()
        print(f"After test insert: {p} page rows")

    # 5. Try raw SQL text insert
    async with AsyncSessionLocal() as db:
        try:
            for i in range(10, 20):
                await db.execute(
                    text("INSERT INTO gsc_page_rows (property_id, page, clicks, impressions, ctr, position) VALUES (:pid, :page, :cl, :im, :ctr, :pos)"),
                    {"pid": PROPERTY_ID, "page": f"https://test.com/raw-{i}", "cl": i, "im": i*10, "ctr": 0.05, "pos": float(i)}
                )
            await db.commit()
            print("Raw text insert: committed")
        except Exception as e:
            print(f"Raw text insert FAILED: {e}")
            import traceback; traceback.print_exc()

    # 6. Final count
    async with AsyncSessionLocal() as db:
        p = (await db.execute(select(func.count()).where(GscPageRow.property_id == PROPERTY_ID))).scalar()
        print(f"Final: {p} page rows")

    # 7. Cleanup
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(GscPageRow).where(GscPageRow.property_id == PROPERTY_ID))
        await db.commit()

asyncio.run(main())

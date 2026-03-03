"""
Clear MongoDB database - DESTRUCTIVE OPERATION

WARNING: This will DELETE ALL interview data!
"""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient


async def clear_database():
    """DANGER: Delete all data from MongoDB"""

    client = AsyncIOMotorClient("mongodb://localhost:27017")
    db = client["interviews"]

    print("=" * 60)
    print("WARNING: This will DELETE ALL DATA!")
    print("=" * 60)

    confirm = input("Type 'DELETE' to confirm: ")

    if confirm != "DELETE":
        print("Aborted.")
        client.close()
        return

    print("\nClearing all data...")

    # Delete all documents from all collections
    collections = ["companies", "jobs", "candidates", "interviews", "transcripts"]

    for collection in collections:
        result = await db[collection].delete_many({})
        print(f"   Deleted {result.deleted_count} documents from {collection}")

    print("\n" + "=" * 60)
    print("Database cleared!")
    print("=" * 60)
    print("\nRun 'uv run python setup_db.py' to recreate sample data")

    client.close()


if __name__ == "__main__":
    asyncio.run(clear_database())
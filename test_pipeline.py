import asyncio
from database import init_db, upsert_jobs, get_unsent_jobs_for_user
from scraper import fetch_all_jobs


async def main():
    init_db()
    jobs = await asyncio.to_thread(
        fetch_all_jobs, country="US", city="New York", tier="white_collar"
    )
    print(f"Fetched {len(jobs)} jobs")
    inserted = upsert_jobs(jobs)
    print(f"Upserted {inserted} rows")


if __name__ == "__main__":
    asyncio.run(main())

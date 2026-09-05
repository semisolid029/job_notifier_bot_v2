import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "JobNotifierBot/1.0"})


def _request_json(url, params=None, timeout=15):
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        logger.warning("Request failed for %s: %s", url, exc)
        return None
    except ValueError as exc:
        logger.warning("Invalid JSON from %s: %s", url, exc)
        return None


def fetch_adzuna_jobs(country="us", keyword="Software Engineer", where=""):
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        logger.info("Adzuna disabled: missing ADZUNA_APP_ID/ADZUNA_APP_KEY")
        return []
    data = _request_json(
        f"https://api.adzuna.com/v1/api/jobs/{country.lower()}/search/1",
        {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "results_per_page": 20,
            "what": keyword,
            "where": where,
            "content-type": "application/json",
        },
    )
    if not data:
        return []
    return [
        {
            "id": f"adzuna_{item.get('id')}",
            "title": item.get("title", "No Title"),
            "company": item.get("company", {}).get("display_name", "Unknown"),
            "location": item.get("location", {}).get("display_name", "Remote"),
            "url": item.get("redirect_url", ""),
            "source": "Adzuna",
            "country": country.upper(),
            "city": where,
            "tier": "white_collar" if "software" in keyword.lower() else "non_tech",
        }
        for item in data.get("results", [])
    ]


def fetch_jobicy_jobs(keyword="dev"):
    data = _request_json(f"https://jobicy.com/api/v2/remote-jobs?count=20&tag={keyword}")
    if not data:
        return []
    return [
        {
            "id": f"jobicy_{item.get('id')}",
            "title": item.get("jobTitle", "No Title"),
            "company": item.get("companyName", "Unknown"),
            "location": item.get("jobGeo", "Remote"),
            "url": item.get("url", ""),
            "source": "Jobicy",
            "country": "",
            "city": "",
            "tier": "white_collar" if keyword == "dev" else "non_tech",
        }
        for item in data.get("jobs", [])
    ]


def fetch_remotive_jobs(keyword="software"):
    data = _request_json(
        "https://remotive.com/api/remote-jobs",
        {"search": keyword, "limit": 20},
    )
    if not data:
        return []
    return [
        {
            "id": f"remotive_{item.get('id')}",
            "title": item.get("title", "No Title"),
            "company": item.get("company_name", "Unknown"),
            "location": item.get("candidate_required_location", "Remote"),
            "url": item.get("url", ""),
            "source": "Remotive",
            "country": "",
            "city": "",
            "tier": "white_collar" if "software" in keyword.lower() else "non_tech",
        }
        for item in data.get("jobs", [])
    ]


def fetch_arbeitnow_jobs(keyword="Software Engineer"):
    data = _request_json("https://arbeitnow.com/api/job-board-api")
    if not data:
        return []
    needle = keyword.lower().strip()
    results = []
    for item in data.get("data", []):
        title = item.get("title", "")
        if needle and needle not in title.lower():
            continue
        results.append(
            {
                "id": f"arbeitnow_{item.get('slug')}",
                "title": title or "No Title",
                "company": item.get("company_name", "Unknown"),
                "location": item.get("location") or ("Remote" if item.get("remote") else "Not specified"),
                "url": item.get("url", ""),
                "source": "Arbeitnow",
                "country": "",
                "city": "",
                "tier": "white_collar" if "software" in keyword.lower() else "non_tech",
            }
        )
        if len(results) >= 20:
            break
    return results


def fetch_all_jobs(country="US", city="", tier="white_collar"):
    keyword = "Software Engineer" if tier != "non_tech" else "Warehouse"
    remote_keyword = "dev" if tier != "non_tech" else "warehouse"

    funcs = [
        lambda: fetch_adzuna_jobs(country=country.lower(), keyword=keyword, where=city),
        lambda: fetch_jobicy_jobs(keyword=remote_keyword),
        lambda: fetch_remotive_jobs(keyword=keyword),
        lambda: fetch_arbeitnow_jobs(keyword=keyword),
    ]

    all_jobs = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fn) for fn in funcs]
        for future in as_completed(futures):
            try:
                all_jobs.extend(future.result())
            except Exception:
                logger.exception("Job provider failed")
    return all_jobs

import os
import json
import gzip
import base64
import logging
import asyncio
from datetime import datetime
from github import Github, GithubException
from bot.db import get_all_users, get_all_downloads, get_cursor

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = os.environ.get("GITHUB_REPO")


def _serialize(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def get_github_repo():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise ValueError("GITHUB_TOKEN and GITHUB_REPO must be set.")
    return Github(GITHUB_TOKEN).get_repo(GITHUB_REPO)


async def create_backup() -> dict:
    loop = asyncio.get_event_loop()

    def _do_backup():
        users     = get_all_users()
        downloads = get_all_downloads()

        with get_cursor() as cur:
            cur.execute("SELECT * FROM premium_keys")
            keys = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM payment_requests")
            payments = [dict(r) for r in cur.fetchall()]

        data = {
            "backup_time":      datetime.now().isoformat(),
            "users":            users,
            "downloads":        downloads,
            "premium_keys":     keys,
            "payment_requests": payments,
        }

        json_str   = json.dumps(data, default=_serialize, indent=2)
        compressed = gzip.compress(json_str.encode("utf-8"))
        encoded    = base64.b64encode(compressed).decode("utf-8")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"backups/backup_{timestamp}.json.gz.b64"

        repo = get_github_repo()
        try:
            existing = repo.get_contents(filename)
            repo.update_file(filename, f"Backup {timestamp}", encoded, existing.sha)
        except GithubException:
            repo.create_file(filename, f"Backup {timestamp}", encoded)

        latest_filename = "backups/latest.json"
        latest_data = json.dumps({
            "latest_backup":  filename,
            "backup_time":    datetime.now().isoformat(),
            "user_count":     len(users),
            "download_count": len(downloads),
        }, indent=2)
        try:
            existing_latest = repo.get_contents(latest_filename)
            repo.update_file(latest_filename, "Update latest pointer", latest_data, existing_latest.sha)
        except GithubException:
            repo.create_file(latest_filename, "Create latest pointer", latest_data)

        logger.info("Backup created: %s (%d users, %d downloads)", filename, len(users), len(downloads))
        return {
            "filename":       filename,
            "user_count":     len(users),
            "download_count": len(downloads),
            "size_kb":        len(compressed) // 1024,
        }

    return await loop.run_in_executor(None, _do_backup)


async def list_backups() -> list:
    loop = asyncio.get_event_loop()

    def _list():
        repo = get_github_repo()
        try:
            contents = repo.get_contents("backups")
            files = [
                {"name": f.name, "path": f.path, "size": f.size, "sha": f.sha}
                for f in contents
                if f.name.startswith("backup_") and f.name.endswith(".b64")
            ]
            return sorted(files, key=lambda x: x["name"], reverse=True)
        except GithubException:
            return []

    return await loop.run_in_executor(None, _list)


async def restore_backup(filename: str) -> dict:
    loop = asyncio.get_event_loop()

    def _restore():
        repo         = get_github_repo()
        file_content = repo.get_contents(filename)
        encoded      = file_content.decoded_content.decode("utf-8")
        compressed   = base64.b64decode(encoded)
        json_str     = gzip.decompress(compressed).decode("utf-8")
        data         = json.loads(json_str)

        restored = {"users": 0, "downloads": 0, "premium_keys": 0}

        with get_cursor() as cur:
            for user in data.get("users", []):
                cur.execute("""
                    INSERT INTO users (
                        user_id, username, first_name, last_name, is_premium,
                        premium_expiry, premium_plan, downloads_used, free_limit,
                        joined_at, last_active, is_banned
                    ) VALUES (
                        %(user_id)s, %(username)s, %(first_name)s, %(last_name)s,
                        %(is_premium)s, %(premium_expiry)s, %(premium_plan)s,
                        %(downloads_used)s, %(free_limit)s, %(joined_at)s,
                        %(last_active)s, %(is_banned)s
                    )
                    ON CONFLICT (user_id) DO UPDATE SET
                        username       = EXCLUDED.username,
                        first_name     = EXCLUDED.first_name,
                        last_name      = EXCLUDED.last_name,
                        is_premium     = EXCLUDED.is_premium,
                        premium_expiry = EXCLUDED.premium_expiry,
                        premium_plan   = EXCLUDED.premium_plan,
                        downloads_used = EXCLUDED.downloads_used,
                        free_limit     = EXCLUDED.free_limit,
                        last_active    = EXCLUDED.last_active,
                        is_banned      = EXCLUDED.is_banned
                """, user)
                if cur.rowcount:
                    restored["users"] += 1

            for dl in data.get("downloads", []):
                cur.execute("""
                    INSERT INTO downloads (id, user_id, url, platform, file_size, status, downloaded_at)
                    VALUES (%(id)s, %(user_id)s, %(url)s, %(platform)s, %(file_size)s,
                            %(status)s, %(downloaded_at)s)
                    ON CONFLICT (id) DO NOTHING
                """, dl)
                if cur.rowcount:
                    restored["downloads"] += 1

            for key in data.get("premium_keys", []):
                cur.execute("""
                    INSERT INTO premium_keys (key, plan, days, created_at, used_by, used_at, is_active)
                    VALUES (%(key)s, %(plan)s, %(days)s, %(created_at)s, %(used_by)s,
                            %(used_at)s, %(is_active)s)
                    ON CONFLICT (key) DO NOTHING
                """, key)
                if cur.rowcount:
                    restored["premium_keys"] += 1

            # Reseed the downloads sequence so future inserts don't collide
            cur.execute("SELECT setval('downloads_id_seq', COALESCE((SELECT MAX(id) FROM downloads), 1))")

        logger.info("Restore complete: %s", restored)
        return restored

    return await loop.run_in_executor(None, _restore)

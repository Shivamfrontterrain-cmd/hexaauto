import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger("autohexa.database")

class Database:
    """Thread-safe SQLite database manager for AutoHexa Multi-User SaaS."""

    def __init__(self, db_path: Path = config.DATABASE_PATH):
        self.db_path = db_path
        self._init_db_sync()

    @contextmanager
    def _connection(self):
        """Context manager that ensures the database connection is cleanly closed."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db_sync(self):
        """Initializes tables and indexes synchronously."""
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_admin INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    account_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    session_string TEXT NOT NULL,
                    is_active INTEGER DEFAULT 0,
                    action_mode TEXT DEFAULT 'guess',
                    hunt_mode TEXT DEFAULT 'catch',
                    hunt_interval REAL DEFAULT 12.0,
                    guess_interval REAL DEFAULT 5.0,
                    target_bot TEXT DEFAULT 'HeXamonbot',
                    hunt_chat TEXT DEFAULT 'HeXamonbot',
                    auto_catch INTEGER DEFAULT 1,
                    fast_battle INTEGER DEFAULT 1,
                    auto_switch INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (owner_id) REFERENCES users(telegram_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS stats (
                    account_id INTEGER PRIMARY KEY,
                    catches INTEGER DEFAULT 0,
                    kills INTEGER DEFAULT 0,
                    guesses INTEGER DEFAULT 0,
                    checks_solved INTEGER DEFAULT 0,
                    pd_earned INTEGER DEFAULT 0,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_id);
                CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active);
            """)
            try:
                conn.execute("ALTER TABLE accounts ADD COLUMN auto_switch INTEGER DEFAULT 1;")
            except sqlite3.OperationalError:
                pass
            conn.commit()
            logger.info("Database initialized successfully at %s", self.db_path)

    # ------------------ USERS ------------------

    def _register_user_sync(self, telegram_id: int, username: str, first_name: str, is_admin: bool):
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO users (telegram_id, username, first_name, is_admin)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    is_admin = CASE WHEN excluded.is_admin = 1 THEN 1 ELSE users.is_admin END
            """, (telegram_id, username, first_name, 1 if is_admin else 0))
            conn.commit()

    async def register_user(self, telegram_id: int, username: str = "", first_name: str = "", is_admin: bool = False):
        await asyncio.to_thread(self._register_user_sync, telegram_id, username, first_name, is_admin)

    def _get_user_sync(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            return dict(row) if row else None

    async def get_user(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_user_sync, telegram_id)

    # ------------------ ACCOUNTS ------------------

    def _add_account_sync(
        self,
        owner_id: int,
        account_name: str,
        phone: str,
        session_string: str,
        action_mode: str = "guess",
        hunt_mode: str = "catch",
        target_bot: str = "HeXamonbot",
        hunt_chat: str = "HeXamonbot"
    ) -> int:
        clean_target = target_bot.lstrip("@")
        clean_hunt = (hunt_chat or target_bot).lstrip("@")
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO accounts (owner_id, account_name, phone, session_string, action_mode, hunt_mode, target_bot, hunt_chat)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (owner_id, account_name, phone, session_string, action_mode, hunt_mode, clean_target, clean_hunt))
            acc_id = cursor.lastrowid
            cursor.execute("INSERT OR IGNORE INTO stats (account_id) VALUES (?)", (acc_id,))
            conn.commit()
            return acc_id

    async def add_account(
        self,
        owner_id: int,
        account_name: str,
        phone: str,
        session_string: str,
        action_mode: str = "guess",
        hunt_mode: str = "catch",
        target_bot: str = "HeXamonbot",
        hunt_chat: str = "HeXamonbot"
    ) -> int:
        return await asyncio.to_thread(
            self._add_account_sync, owner_id, account_name, phone, session_string, action_mode, hunt_mode, target_bot, hunt_chat
        )

    def _get_account_sync(self, account_id: int) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            return dict(row) if row else None

    async def get_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_account_sync, account_id)

    def _get_user_accounts_sync(self, owner_id: int) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE owner_id = ? ORDER BY id ASC",
                (owner_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    async def get_user_accounts(self, owner_id: int) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_user_accounts_sync, owner_id)

    def _get_all_running_accounts_sync(self) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM accounts WHERE is_active = 1").fetchall()
            return [dict(r) for r in rows]

    async def get_all_running_accounts(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._get_all_running_accounts_sync)

    def _set_account_active_sync(self, account_id: int, is_active: bool):
        with self._connection() as conn:
            conn.execute(
                "UPDATE accounts SET is_active = ? WHERE id = ?",
                (1 if is_active else 0, account_id)
            )
            conn.commit()

    async def set_account_active(self, account_id: int, is_active: bool):
        await asyncio.to_thread(self._set_account_active_sync, account_id, is_active)

    def _update_account_settings_sync(self, account_id: int, **kwargs):
        allowed_fields = {
            "account_name", "action_mode", "hunt_mode", "hunt_interval",
            "guess_interval", "target_bot", "hunt_chat", "auto_catch", "fast_battle", "auto_switch"
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not updates:
            return
        if "target_bot" in updates and isinstance(updates["target_bot"], str):
            updates["target_bot"] = updates["target_bot"].lstrip("@")
        if "hunt_chat" in updates and isinstance(updates["hunt_chat"], str):
            updates["hunt_chat"] = updates["hunt_chat"].lstrip("@")
        set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [account_id]
        with self._connection() as conn:
            conn.execute(f"UPDATE accounts SET {set_clause} WHERE id = ?", values)
            conn.commit()

    async def update_account_settings(self, account_id: int, **kwargs):
        await asyncio.to_thread(self._update_account_settings_sync, account_id, **kwargs)

    def _delete_account_sync(self, account_id: int, owner_id: int) -> bool:
        with self._connection() as conn:
            res = conn.execute(
                "DELETE FROM accounts WHERE id = ? AND owner_id = ?",
                (account_id, owner_id)
            )
            conn.commit()
            return res.rowcount > 0

    async def delete_account(self, account_id: int, owner_id: int) -> bool:
        return await asyncio.to_thread(self._delete_account_sync, account_id, owner_id)

    # ------------------ STATS ------------------

    def _increment_stat_sync(self, account_id: int, stat_name: str, amount: int = 1):
        allowed_stats = {"catches", "kills", "guesses", "checks_solved", "pd_earned"}
        if stat_name not in allowed_stats:
            return
        with self._connection() as conn:
            conn.execute(f"""
                INSERT INTO stats (account_id, {stat_name}, last_active)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(account_id) DO UPDATE SET
                    {stat_name} = stats.{stat_name} + ?,
                    last_active = CURRENT_TIMESTAMP
            """, (account_id, amount, amount))
            conn.commit()

    async def increment_stat(self, account_id: int, stat_name: str, amount: int = 1):
        await asyncio.to_thread(self._increment_stat_sync, account_id, stat_name, amount)

    def _get_stats_sync(self, account_id: int) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM stats WHERE account_id = ?", (account_id,)).fetchone()
            if row:
                return dict(row)
            return {"catches": 0, "kills": 0, "guesses": 0, "checks_solved": 0, "pd_earned": 0}

    async def get_stats(self, account_id: int) -> Dict[str, Any]:
        return await asyncio.to_thread(self._get_stats_sync, account_id)

    def _get_user_total_stats_sync(self, owner_id: int) -> Dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("""
                SELECT 
                    COALESCE(SUM(s.catches), 0) as total_catches,
                    COALESCE(SUM(s.kills), 0) as total_kills,
                    COALESCE(SUM(s.guesses), 0) as total_guesses,
                    COALESCE(SUM(s.checks_solved), 0) as total_checks,
                    COALESCE(SUM(s.pd_earned), 0) as total_pd
                FROM accounts a
                LEFT JOIN stats s ON a.id = s.account_id
                WHERE a.owner_id = ?
            """, (owner_id,)).fetchone()
            return dict(row) if row else {
                "total_catches": 0, "total_kills": 0, "total_guesses": 0,
                "total_checks": 0, "total_pd": 0
            }

    async def get_user_total_stats(self, owner_id: int) -> Dict[str, Any]:
        return await asyncio.to_thread(self._get_user_total_stats_sync, owner_id)

# Global singleton instance
db = Database()

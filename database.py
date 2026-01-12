import os
import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager

# Detect environment
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
IS_POSTGRES = bool(DATABASE_URL)

if IS_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

DB_NAME = "users.db"

@contextmanager
def get_db_cursor(commit=False):
    """
    Context manager to yield a cursor and handle commit/close.
    Supports both SQLite and PostgreSQL.
    """
    conn = None
    try:
        if IS_POSTGRES:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        else:
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
        
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
            
    except Exception as e:
        logger.error(f"Database Error: {e}")
        if conn: conn.rollback()
        raise e
    finally:
        if conn: conn.close()

def get_placeholder():
    return "%s" if IS_POSTGRES else "?"

def init_db():
    p = get_placeholder()
    SERIAL_TYPE = "SERIAL" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    with get_db_cursor(commit=True) as c:
        if not IS_POSTGRES:
            c.execute('PRAGMA journal_mode=WAL;')

        # 1. Users Table
        # Note: Postgres doesn't strictly need IF NOT EXISTS in all versions but it's standard.
        # SQLite uses TEXT for everything, Postgres has specific types but TEXT works for both roughly.
        # FIX: Postgres strict boolean check. DEFAULT 0 is invalid for BOOLEAN. Use FALSE.
        c.execute(f'''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                usage_count INTEGER DEFAULT 0,
                is_paid BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # 2. Tasks Table
        c.execute(f'''
            CREATE TABLE IF NOT EXISTS tasks (
                id {SERIAL_TYPE},
                user_id TEXT,
                task_content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_digest_sent BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # 3. Migrations
        # PostgreSQL doesn't support "PRAGMA table_info". We need a different check.
        # Simplified migration strategy: Try to add column and ignore error if exists.
        
        columns = [
            ('stripe_customer_id', 'TEXT'),
            ('minutes_used', 'REAL DEFAULT 0.0'),
            ('plan_tier', 'TEXT DEFAULT \'free\''), # Standard SQL quotes
            ('last_reset_date', 'TEXT'),
            ('todoist_token', 'TEXT'),
            ('notion_token', 'TEXT'),
            ('notion_page_id', 'TEXT'),
            ('digest_time', 'TEXT DEFAULT \'18:00\''),
            ('timezone', 'TEXT DEFAULT \'UTC\'')
        ]
        
        # Using a brute-force approach for migrations that works on both without complex schema inspection
        for col_name, col_type in columns:
            try:
                c.execute(f'ALTER TABLE users ADD COLUMN {col_name} {col_type}')
            except Exception as e:
                # Ignore "Duplicate column" errors
                pass

def check_user_status(user_id):
    p = get_placeholder()
    with get_db_cursor(commit=True) as c:
        c.execute(f'SELECT * FROM users WHERE user_id = {p}', (str(user_id),))
        row = c.fetchone()
        
        if row is None:
            # FIX: Postgres strict boolean. Use FALSE instead of 0.
            c.execute(f'INSERT INTO users (user_id, usage_count, is_paid, plan_tier, minutes_used) VALUES ({p}, 0, FALSE, \'free\', 0.0)', (str(user_id),))
            # No need to commit here, context manager does it
            
            c.execute(f'SELECT * FROM users WHERE user_id = {p}', (str(user_id),))
            row = c.fetchone()
            
        return dict(row)

def update_user(user_id, **kwargs):
    p = get_placeholder()
    sets = []
    values = []
    for key, val in kwargs.items():
        sets.append(f"{key} = {p}")
        values.append(val)
    
    values.append(str(user_id))
    
    query = f"UPDATE users SET {', '.join(sets)} WHERE user_id = {p}"
    
    with get_db_cursor(commit=True) as c:
        c.execute(query, values)

def add_task(user_id, content):
    p = get_placeholder()
    with get_db_cursor(commit=True) as c:
        c.execute(f'INSERT INTO tasks (user_id, task_content) VALUES ({p}, {p})', (str(user_id), content))

def get_unsent_tasks(user_id):
    p = get_placeholder()
    with get_db_cursor(commit=False) as c:
        c.execute(f'SELECT * FROM tasks WHERE user_id = {p} AND is_digest_sent = 0', (str(user_id),))
        rows = c.fetchall()
        return [dict(row) for row in rows]

def mark_tasks_sent(task_ids):
    if not task_ids: return
    p = get_placeholder()
    
    # Postgres uses ANY(%s) for array/list or standard IN clause
    # SQLite only supports IN (?, ?, ?)
    # We stick to the generic IN clause builder
    
    placeholders = ','.join([p] * len(task_ids))
    with get_db_cursor(commit=True) as c:
        # Cast Is_Digest_Sent to proper type/value logic handled by driver usually
        # Postgres BOOLEAN accepts 1/0 or True/False. SQLite 1/0.
        c.execute(f'UPDATE tasks SET is_digest_sent = 1 WHERE id IN ({placeholders})', tuple(task_ids))

def get_all_users():
    with get_db_cursor(commit=False) as c:
        c.execute('SELECT * FROM users')
        rows = c.fetchall()
        return [dict(r) for r in rows]

def get_user_by_stripe_id(stripe_customer_id):
    p = get_placeholder()
    with get_db_cursor(commit=False) as c:
        c.execute(f'SELECT * FROM users WHERE stripe_customer_id = {p}', (str(stripe_customer_id),))
        row = c.fetchone()
        return dict(row) if row else None

if __name__ == '__main__':
    # For local test
    init_db()
    print("Database initialized.")

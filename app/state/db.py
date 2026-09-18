import aiosqlite
from datetime import datetime
import pytz

DB_PATH = "ttdaytrader_state.db"

class StateManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    async def initialize_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS portfolio (
                    id INTEGER PRIMARY KEY,
                    settled_cash REAL,
                    locked_cash REAL
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS active_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    shares REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    target REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    status TEXT DEFAULT 'OPEN',
                    exit_price REAL,
                    exit_time TEXT,
                    exit_reason TEXT,
                    realized_pnl REAL,
                    orig_shares REAL,
                    unit REAL,
                    scaled INTEGER DEFAULT 0,
                    scaled_shares REAL DEFAULT 0.0,
                    scaled_price REAL DEFAULT 0.0,
                    scaled_pnl REAL DEFAULT 0.0
                )
            ''')
            # Ensure partial scaling columns exist if migrating an existing database
            async with db.execute('PRAGMA table_info(active_positions)') as cursor:
                columns = [row[1] for row in await cursor.fetchall()]

            if 'orig_shares' not in columns:
                await db.execute('ALTER TABLE active_positions ADD COLUMN orig_shares REAL')
            if 'unit' not in columns:
                await db.execute('ALTER TABLE active_positions ADD COLUMN unit REAL')
            if 'scaled' not in columns:
                await db.execute('ALTER TABLE active_positions ADD COLUMN scaled INTEGER DEFAULT 0')
            if 'scaled_shares' not in columns:
                await db.execute('ALTER TABLE active_positions ADD COLUMN scaled_shares REAL DEFAULT 0.0')
            if 'scaled_price' not in columns:
                await db.execute('ALTER TABLE active_positions ADD COLUMN scaled_price REAL DEFAULT 0.0')
            if 'scaled_pnl' not in columns:
                await db.execute('ALTER TABLE active_positions ADD COLUMN scaled_pnl REAL DEFAULT 0.0')
            if 'strategy' not in columns:
                await db.execute("ALTER TABLE active_positions ADD COLUMN strategy TEXT DEFAULT 'MORNING_MOMENTUM'")

            await db.execute('UPDATE active_positions SET orig_shares = shares WHERE orig_shares IS NULL')

            async with db.execute('SELECT count(*) FROM portfolio') as cursor:
                row = await cursor.fetchone()
                if row[0] == 0:
                    await db.execute(
                        'INSERT INTO portfolio (settled_cash, locked_cash) VALUES (?, ?)',
                        (1000.0, 0.0)
                    )
            await db.commit()

    async def get_trades_count_today(self):
        """Counts how many trades have been opened today for the scaling limit."""
        tz = pytz.timezone('America/New_York')
        today_str = datetime.now(tz).strftime('%Y-%m-%d')
        
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('''
                SELECT count(*) FROM active_positions 
                WHERE entry_time LIKE ?
            ''', (today_str + '%',)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    async def get_available_cash(self):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT settled_cash FROM portfolio WHERE id = 1') as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0.0

    async def check_settled_cash_available(self):
        cash = await self.get_available_cash()
        return cash > 10.00

    async def lock_capital(self, amount: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE portfolio 
                SET settled_cash = settled_cash - ?, locked_cash = locked_cash + ? 
                WHERE id = 1
            ''', (amount, amount))
            await db.commit()

    async def release_capital(self, amount: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE portfolio 
                SET settled_cash = settled_cash + ?, locked_cash = locked_cash - ? 
                WHERE id = 1
            ''', (amount, amount))
            await db.commit()

    async def open_position(self, ticker: str, shares: float, entry_price: float, stop_loss: float, target: float, entry_time: str, orig_shares: float = None, unit: float = None, strategy: str = 'MORNING_MOMENTUM'):
        if orig_shares is None:
            orig_shares = shares
        if unit is None:
            unit = target - entry_price
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                INSERT INTO active_positions (ticker, shares, entry_price, stop_loss, target, entry_time, status, orig_shares, unit, scaled, scaled_shares, scaled_price, scaled_pnl, strategy)
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, 0, 0.0, 0.0, 0.0, ?)
            ''', (ticker, shares, entry_price, stop_loss, target, entry_time, orig_shares, unit, strategy))
            await db.commit()
            return cursor.lastrowid

    async def record_partial_exit(self, ticker: str, scaled_shares: float, remaining_shares: float, scaled_price: float, scaled_pnl: float, new_stop_loss: float):
        """Records partial scale-out execution in SQLite and updates stop loss to breakeven."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE active_positions
                SET shares = ?,
                    stop_loss = ?,
                    scaled = 1,
                    scaled_shares = ?,
                    scaled_price = ?,
                    scaled_pnl = ?
                WHERE ticker = ? AND status = 'OPEN'
            ''', (remaining_shares, new_stop_loss, scaled_shares, scaled_price, scaled_pnl, ticker))
            await db.commit()

    async def update_stop_loss(self, ticker: str, new_stop_loss: float):
        """Updates the stop loss price for an open position upon ratchet activation."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE active_positions
                SET stop_loss = ?
                WHERE ticker = ? AND status = 'OPEN'
            ''', (new_stop_loss, ticker))
            await db.commit()

    async def close_position(self, ticker: str, exit_price: float, exit_time: str, exit_reason: str, realized_pnl: float, cost_basis: float, proceeds: float):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE active_positions
                SET status = 'CLOSED',
                    exit_price = ?,
                    exit_time = ?,
                    exit_reason = ?,
                    realized_pnl = ?
                WHERE ticker = ? AND status = 'OPEN'
            ''', (exit_price, exit_time, exit_reason, realized_pnl, ticker))

            await db.execute('''
                UPDATE portfolio
                SET locked_cash = MAX(0.0, locked_cash - ?),
                    settled_cash = settled_cash + ?
                WHERE id = 1
            ''', (cost_basis, proceeds))
            await db.commit()

    async def get_open_positions(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('''
                SELECT id, ticker, shares, entry_price, stop_loss, target, entry_time, status,
                       orig_shares, unit, scaled, scaled_shares, scaled_price, scaled_pnl, strategy
                FROM active_positions
                WHERE status = 'OPEN'
            ''') as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
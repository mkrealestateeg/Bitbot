from __future__ import annotations
import json,math,sqlite3
from pathlib import Path
from datetime import datetime,timezone,date
from .local_learning import FEATURE_NAMES,MODEL_VERSION
class Database:
    def __init__(self,path):
        self.path=path
        if path!=":memory:":Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(path,check_same_thread=False);self.conn.row_factory=sqlite3.Row;self.conn.execute("PRAGMA journal_mode=WAL");self._init()
    def _init(self):
        self.conn.executescript("""CREATE TABLE IF NOT EXISTS symbols(symbol TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1,added_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,symbol TEXT NOT NULL,direction TEXT NOT NULL,price REAL,score REAL,setup TEXT,reason TEXT,snapshot TEXT);
        CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,ts_open TEXT NOT NULL,ts_close TEXT,symbol TEXT NOT NULL,direction TEXT NOT NULL,entry REAL NOT NULL,stop REAL NOT NULL,target REAL NOT NULL,qty REAL NOT NULL,status TEXT NOT NULL,mode TEXT NOT NULL,pnl REAL,r_multiple REAL,source TEXT NOT NULL DEFAULT 'PAPER',close_reason TEXT,snapshot TEXT,initial_risk REAL NOT NULL DEFAULT 0,management_stage INTEGER NOT NULL DEFAULT 0,favorable_price REAL,protection_order_id TEXT,protection_client_oid TEXT);
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_stats(day TEXT PRIMARY KEY,realized_pnl REAL NOT NULL DEFAULT 0,trades INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS trade_followups(
            trade_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            text_hash TEXT,
            near_target INTEGER NOT NULL DEFAULT 0,
            tp1_reached INTEGER NOT NULL DEFAULT 0,
            breakeven INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(trade_id,chat_id)
        );""")
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(trades)")
        }
        migrations = {
            "initial_risk": "ALTER TABLE trades ADD COLUMN initial_risk REAL NOT NULL DEFAULT 0",
            "management_stage": "ALTER TABLE trades ADD COLUMN management_stage INTEGER NOT NULL DEFAULT 0",
            "favorable_price": "ALTER TABLE trades ADD COLUMN favorable_price REAL",
            "protection_order_id": "ALTER TABLE trades ADD COLUMN protection_order_id TEXT",
            "protection_client_oid": "ALTER TABLE trades ADD COLUMN protection_client_oid TEXT",
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.conn.execute(statement)
        self.conn.execute(
            "UPDATE trades SET initial_risk=ABS(entry-stop) "
            "WHERE initial_risk<=0"
        )
        self.conn.execute(
            "UPDATE trades SET favorable_price=entry "
            "WHERE favorable_price IS NULL"
        )
        self.conn.commit()
    def seed_symbols(self,symbols):
        now=datetime.now(timezone.utc).isoformat()
        for s in symbols:self.conn.execute("INSERT OR IGNORE INTO symbols(symbol,enabled,added_at) VALUES(?,1,?)",(s,now))
        self.conn.commit()
    def list_symbols(self):return[dict(r) for r in self.conn.execute("SELECT symbol,enabled FROM symbols ORDER BY symbol")]
    def enabled_symbols(self):return[r[0] for r in self.conn.execute("SELECT symbol FROM symbols WHERE enabled=1 ORDER BY symbol")]
    def add_symbol(self,symbol):self.conn.execute("INSERT INTO symbols(symbol,enabled,added_at) VALUES(?,1,?) ON CONFLICT(symbol) DO UPDATE SET enabled=1",(symbol,datetime.now(timezone.utc).isoformat()));self.conn.commit()
    def set_symbol(self,symbol,enabled):cur=self.conn.execute("UPDATE symbols SET enabled=? WHERE symbol=?",(int(enabled),symbol));self.conn.commit();return cur.rowcount>0
    def remove_symbol(self,symbol):
        if self.conn.execute("SELECT 1 FROM trades WHERE symbol=? AND status='OPEN' LIMIT 1",(symbol,)).fetchone():return False
        cur=self.conn.execute("DELETE FROM symbols WHERE symbol=?",(symbol,));self.conn.commit();return cur.rowcount>0
    def save_signal(self,symbol,direction,price,score,setup,reason,snapshot):
        cur=self.conn.execute("INSERT INTO signals(ts,symbol,direction,price,score,setup,reason,snapshot) VALUES(?,?,?,?,?,?,?,?)",(datetime.now(timezone.utc).isoformat(),symbol,direction,price,score,setup,reason,json.dumps(snapshot,default=str)));self.conn.commit();return cur.lastrowid
    def open_trade(
        self,
        symbol,
        direction,
        entry,
        stop,
        target,
        qty,
        mode="MANUAL",
        source="PAPER",
        snapshot=None,
        protection_order_id=None,
        protection_client_oid=None,
    ):
        cur = self.conn.execute(
            "INSERT INTO trades("
            "ts_open,symbol,direction,entry,stop,target,qty,status,mode,"
            "source,snapshot,initial_risk,favorable_price,"
            "protection_order_id,protection_client_oid"
            ") VALUES(?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                symbol,
                direction,
                entry,
                stop,
                target,
                qty,
                mode,
                source,
                json.dumps(snapshot or {}, default=str),
                abs(float(entry) - float(stop)),
                float(entry),
                protection_order_id,
                protection_client_oid,
            ),
        )
        self.conn.commit()
        return cur.lastrowid
    def open_trades(self,source=None):
        if source is None:return[dict(r) for r in self.conn.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id")]
        return[dict(r) for r in self.conn.execute("SELECT * FROM trades WHERE status='OPEN' AND source=? ORDER BY id",(str(source).upper(),))]
    def trade_followup(self,trade_id,chat_id):
        row=self.conn.execute("SELECT * FROM trade_followups WHERE trade_id=? AND chat_id=?",(trade_id,chat_id)).fetchone()
        return dict(row) if row else None
    def update_trade_stop(self,trade_id,stop):
        row=self.conn.execute("SELECT direction,entry,stop FROM trades WHERE id=? AND status='OPEN'",(trade_id,)).fetchone()
        if not row:return False
        stop=float(stop)
        if (row["direction"]=="LONG" and stop<row["stop"]) or (row["direction"]=="SHORT" and stop>row["stop"]):return False
        cur=self.conn.execute("UPDATE trades SET stop=? WHERE id=? AND status='OPEN'",(stop,trade_id));self.conn.commit();return cur.rowcount>0
    def save_trade_management(
        self,
        trade_id,
        stage,
        favorable_price,
        stop,
        protection_order_id=None,
        protection_client_oid=None,
    ):
        row = self.conn.execute(
            "SELECT direction,stop,management_stage,favorable_price "
            "FROM trades WHERE id=? AND status='OPEN'",
            (trade_id,),
        ).fetchone()
        if not row:
            return False

        direction = str(row["direction"]).upper()
        stage = int(stage)
        stop = float(stop)
        favorable_price = float(favorable_price)
        previous_stop = float(row["stop"])
        previous_favorable = float(row["favorable_price"] or 0)
        if (
            direction not in {"LONG", "SHORT"}
            or stage not in {0, 1, 2, 3}
            or stage < int(row["management_stage"])
            or not math.isfinite(stop)
            or not math.isfinite(favorable_price)
            or stop <= 0
            or favorable_price <= 0
        ):
            return False
        if (
            (direction == "LONG" and stop < previous_stop)
            or (direction == "SHORT" and stop > previous_stop)
        ):
            return False
        if (
            (direction == "LONG" and favorable_price < previous_favorable)
            or (
                direction == "SHORT"
                and previous_favorable > 0
                and favorable_price > previous_favorable
            )
        ):
            return False

        cur = self.conn.execute(
            "UPDATE trades SET management_stage=?,favorable_price=?,stop=?,"
            "protection_order_id=COALESCE(?,protection_order_id),"
            "protection_client_oid=COALESCE(?,protection_client_oid) "
            "WHERE id=? AND status='OPEN'",
            (
                stage,
                favorable_price,
                stop,
                protection_order_id,
                protection_client_oid,
                trade_id,
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0
    def save_trade_followup(self,trade_id,chat_id,message_id,text_hash):
        self.conn.execute("INSERT INTO trade_followups(trade_id,chat_id,message_id,text_hash) VALUES(?,?,?,?) ON CONFLICT(trade_id,chat_id) DO UPDATE SET message_id=excluded.message_id,text_hash=excluded.text_hash",(trade_id,chat_id,message_id,text_hash));self.conn.commit()
    def mark_trade_followup_event(self,trade_id,chat_id,event):
        columns={"near_target":"near_target","tp1_reached":"tp1_reached","breakeven":"breakeven"}
        column=columns.get(event)
        if not column:raise ValueError(f"Unknown trade follow-up event: {event}")
        self.conn.execute(f"INSERT INTO trade_followups(trade_id,chat_id,{column}) VALUES(?,?,1) ON CONFLICT(trade_id,chat_id) DO UPDATE SET {column}=1",(trade_id,chat_id));self.conn.commit()
    def close_trade(self,trade_id,exit_price,pnl,reason):
        row=self.conn.execute("SELECT entry,stop,direction FROM trades WHERE id=? AND status='OPEN'",(trade_id,)).fetchone()
        if not row:return False
        risk=abs(row["entry"]-row["stop"]);move=(exit_price-row["entry"])*(1 if row["direction"]=="LONG" else -1);r=move/risk if risk else 0.0;now=datetime.now(timezone.utc)
        self.conn.execute("UPDATE trades SET ts_close=?,status='CLOSED',pnl=?,r_multiple=?,close_reason=? WHERE id=?",(now.isoformat(),pnl,r,reason,trade_id))
        day=now.date().isoformat();self.conn.execute("INSERT INTO daily_stats(day,realized_pnl,trades) VALUES(?,?,1) ON CONFLICT(day) DO UPDATE SET realized_pnl=realized_pnl+excluded.realized_pnl,trades=trades+1",(day,pnl));self.conn.commit();return True
    def set_value(self,key,value):
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key),str(value)),
        )
        self.conn.commit()
    def get_value(self,key):
        row=self.conn.execute("SELECT value FROM kv WHERE key=?",(str(key),)).fetchone()
        return row["value"] if row else None
    def local_learning_examples(self,source):
        rows=self.conn.execute(
            "SELECT id,pnl,snapshot FROM trades "
            "WHERE status='CLOSED' AND source=? AND pnl IS NOT NULL AND pnl!=0 "
            "ORDER BY ts_close,id",
            (str(source).upper(),),
        ).fetchall()
        examples=[]
        for row in rows:
            try:
                snapshot=json.loads(row["snapshot"] or "{}")
            except (TypeError,ValueError):
                continue
            learning=snapshot.get("local_learning")
            if not isinstance(learning,dict):
                continue
            if learning.get("schema_version")!=MODEL_VERSION:
                continue
            if learning.get("feature_names")!=list(FEATURE_NAMES):
                continue
            features=learning.get("features")
            if (
                not isinstance(features,list)
                or len(features)!=len(FEATURE_NAMES)
                or not math.isfinite(float(row["pnl"]))
                or any(
                    not isinstance(value,(int,float))
                    or not math.isfinite(float(value))
                    for value in features
                )
            ):
                continue
            examples.append({
                "id":int(row["id"]),
                "pnl":float(row["pnl"]),
                "learning":learning,
            })
        return examples
    def recent_trades(self,limit=10):return[dict(r) for r in self.conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?",(limit,))]
    def daily_pnl(self):
        row=self.conn.execute("SELECT realized_pnl FROM daily_stats WHERE day=?",(date.today().isoformat(),)).fetchone();return float(row[0]) if row else 0.0
    def stats(self):
        rows=self.conn.execute("SELECT pnl,r_multiple FROM trades WHERE status='CLOSED'").fetchall()
        if not rows:return {"closed":0,"win_rate":0.0,"pnl":0.0,"avg_r":0.0,"profit_factor":0.0}
        pnls=[float(r["pnl"] or 0) for r in rows];wins=[p for p in pnls if p>0];losses=[p for p in pnls if p<0]
        return {"closed":len(rows),"win_rate":len(wins)/len(rows)*100,"pnl":sum(pnls),"avg_r":sum(float(r["r_multiple"] or 0) for r in rows)/len(rows),"profit_factor":sum(wins)/abs(sum(losses)) if losses else(999.0 if wins else 0.0)}
    def close(self):
        self.conn.close()

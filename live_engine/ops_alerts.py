"""Read-only ledger watchdog + durable Telegram outbox; no exchange/order access."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time

import requests

from live_engine.state_store import StateStore


def stamp(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat(timespec='seconds')


def load_config(path):
    c=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if c.get('schema')!=1 or not re.fullmatch(r'[a-z0-9-]{1,64}',c.get('monitor_id','')):
        raise ValueError('Invalid monitor identity/schema')
    if type(c.get('poll_seconds')) is not int or not 5<=c['poll_seconds']<=60:
        raise ValueError('Invalid poll interval')
    if type(c.get('reminder_seconds')) is not int or not 300<=c['reminder_seconds']<=86400:
        raise ValueError('Invalid reminder interval')
    if not isinstance(c.get('targets'),list) or not c['targets']:raise ValueError('No monitor targets')
    seen=set()
    for t in c['targets']:
        if (not re.fullmatch(r'[a-z0-9-]{1,64}',t.get('id','')) or t['id'] in seen
            or not re.fullmatch(r'[A-Za-z0-9 _/-]{1,80}',t.get('label',''))
            or t.get('kind') not in ('heartbeat','futures_executor')
            or not isinstance(t.get('path'),str) or not t['path']
            or not isinstance(t.get('expected_metadata'),dict) or not t['expected_metadata'].get('owner')):
            raise ValueError('Invalid target identity/kind/path')
        if (not isinstance(t.get('heartbeat_key'),str) or type(t.get('max_age_seconds')) is not int
            or not 15<=t['max_age_seconds']<=3600):raise ValueError('Invalid target heartbeat')
        seen.add(t['id'])
    return c


def issue(code,detail,delay=0,severity='CRITICAL'):
    return dict(code=code,detail=detail,delay_ms=delay*1000,severity=severity)


def inspect_target(target,now):
    """The only target DB access is URI mode=ro plus query_only and one snapshot."""
    try:
        with sqlite3.connect(Path(target['path']).resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            meta=dict(db.execute('SELECT key,value FROM metadata'))
            if any(meta.get(k)!=v for k,v in target['expected_metadata'].items()):
                return [issue('IDENTITY_MISMATCH','Identitas database berubah; periksa deployment/config.')],False
            age=now-int(meta[target['heartbeat_key']])
            issues=[]
            if age < -5000:issues.append(issue('CLOCK_FUTURE','Timestamp heartbeat di masa depan; periksa waktu server.'))
            elif age>target['max_age_seconds']*1000:
                issues.append(issue('HEARTBEAT_STALE','Worker/sumber tidak memperbarui heartbeat; periksa container dan koneksi.'))
            if target['kind']=='heartbeat':return issues,True
            if meta.get('worker_armed')!='1':
                issues.append(issue('EXECUTOR_DISARMED','Executor tidak armed. Jika tidak disengaja, periksa mode deployment.',60,'WARNING'))
            if meta.get('worker_error'):
                issues.append(issue('WORKER_ERROR','Error worker menetap; lihat status/log terlokal sebelum melanjutkan.',60))
            if meta.get('entries_blocked'):
                issues.append(issue('ENTRIES_BLOCKED','Entry diblokir dan memerlukan review operator; jangan reset ledger.'))
            active=db.execute("SELECT session,plan,state FROM sessions WHERE state NOT IN ('CLOSED','SKIPPED','DISARMED')").fetchall()
            if len(active)>1:issues.append(issue('MULTIPLE_ACTIVE','Lebih dari satu lifecycle aktif pada satu allocation.'))
            for s in active:
                orders=db.execute('SELECT role,state,response,cancel_pending,error FROM orders WHERE session_id=?',(s['session'],)).fetchall()
                if any(r['state'] in ('SENDING','UNKNOWN') or r['cancel_pending'] or r['error'] for r in orders):
                    issues.append(issue('ORDER_UNRESOLVED','Order/cancel belum pasti; cek posisi dan order Binance sebelum tindakan manual.',30))
                entered=any(r['role']=='ENTRY' and r['response'] and float(json.loads(r['response']).get('executedQty','0'))>0 for r in orders)
                protected=any(r['role']=='STOP' and r['state'] in ('NEW','TRIGGERING','TRIGGERED','FINISHED') for r in orders)
                if entered and not protected:
                    issues.append(issue('STOP_UNCONFIRMED','Ledger entry terisi tetapi STOP belum terkonfirmasi; periksa Binance segera.',10))
                if s['plan']:
                    plan=json.loads(s['plan'])
                    if now>int(plan['force_exit_ms'])+15000:
                        issues.append(issue('DEADLINE_UNRESOLVED','Deadline exit lewat tetapi ledger belum CLOSED; periksa posisi/order Binance.'))
            return list({r['code']:r for r in issues}.values()),True
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError,OverflowError):
        return [issue('DB_UNREADABLE','Database/status tidak dapat dibaca; periksa mount, disk dan proses.',30)],False


class DeliveryError(RuntimeError):
    def __init__(self,kind,retry_after=30):
        self.retry_after=retry_after
        super().__init__(kind)  # Fixed categories only; never raw HTTP exception/response.


def credentials(require_chat=True):
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip();chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if os.getenv('TELEGRAM_ENABLED','').lower()!='true':raise ValueError('Telegram is not enabled')
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}',token):
        raise ValueError('Telegram token missing/invalid; configure on server only')
    if require_chat and (not re.fullmatch(r'-?[0-9]+',chat) or int(chat)==0):
        raise ValueError('Telegram credentials missing/invalid; configure on server only')
    return token,int(chat) if require_chat else None


def telegram_call(http,token,method,parameters):
    if method not in ('sendMessage','getMe','getWebhookInfo','getUpdates'):raise ValueError('Telegram method not allowed')
    try:
        r=http.post('https://api.telegram.org/bot'+token+'/'+method,
            json=parameters,timeout=(5,10),allow_redirects=False)
        payload=r.json()
    except (requests.RequestException,ValueError):raise DeliveryError('telegram_transport_or_json') from None
    if r.status_code!=200 or not isinstance(payload,dict) or payload.get('ok') is not True:
        retry=payload.get('parameters',{}).get('retry_after',30) if isinstance(payload,dict) and isinstance(payload.get('parameters',{}),dict) else 30
        retry=retry if type(retry) is int and retry>0 else 30
        raise DeliveryError('telegram_http_'+str(r.status_code),max(5,min(retry,86400)))
    return payload.get('result')


def send_telegram(http,token,chat,text):
    if not isinstance(text,str) or not 1<=len(text)<=4000:raise ValueError('Invalid alert message length')
    result=telegram_call(http,token,'sendMessage',dict(chat_id=chat,text=text,disable_notification=False))
    if (not isinstance(result,dict) or type(result.get('message_id')) is not int
        or not isinstance(result.get('chat'),dict) or result['chat'].get('id')!=chat):
        raise DeliveryError('telegram_recipient_or_ack_mismatch')
    return result['message_id']


def discover(http,token):
    bot=telegram_call(http,token,'getMe',{})
    hook=telegram_call(http,token,'getWebhookInfo',{})
    if not isinstance(bot,dict) or bot.get('is_bot') is not True or not isinstance(hook,dict):
        raise DeliveryError('telegram_identity_invalid')
    if hook.get('url'):raise ValueError('Bot already has webhook; it was NOT removed')
    updates=telegram_call(http,token,'getUpdates',dict(timeout=0,limit=100))
    if not isinstance(updates,list):raise DeliveryError('telegram_updates_invalid')
    chats=set()
    for row in updates:
        msg=row.get('message') if isinstance(row,dict) else None
        chat=msg.get('chat') if isinstance(msg,dict) else None
        if isinstance(chat,dict) and chat.get('type')=='private' and type(chat.get('id')) is int:chats.add(chat['id'])
    return dict(private_chat_ids=sorted(chats),updates_acknowledged=False,
                instruction='Pilih chat pribadi Anda, simpan ID di env server. Isi pesan tidak ditampilkan.')


class AlertStore:
    acquire_writer=StateStore.acquire_writer
    transaction=StateStore.transaction
    def __init__(self,path,monitor_id):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.writer_lock=None
        self.db=sqlite3.connect(path,isolation_level=None,timeout=5);self.db.row_factory=sqlite3.Row
        try:
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                old=dict(self.db.execute('SELECT key,value FROM metadata'))
                if old.get('owner')!='ops_alerts' or old.get('monitor_id')!=monitor_id:
                    raise ValueError('Wrong alert DB identity; never use a trading DB')
            elif self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError('Not an empty alert database')
            self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS conditions(id TEXT PRIMARY KEY,target TEXT,code TEXT,first_ms INTEGER,
                active INTEGER,last_queued_ms INTEGER,detail TEXT,severity TEXT);
              CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,event_id TEXT UNIQUE,condition_id TEXT,
                generation_ms INTEGER,kind TEXT,text TEXT,created_ms INTEGER,next_ms INTEGER,attempts INTEGER DEFAULT 0,
                status TEXT DEFAULT 'PENDING',delivered_ms INTEGER,message_id INTEGER);
            ''')
            with self.transaction():
                self.set_meta('owner','ops_alerts');self.set_meta('monitor_id',monitor_id)
        except BaseException:self.close();raise

    def set_meta(self,key,value):
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

    def queue(self,cid,kind,text,now,generation):
        identity=hashlib.sha256((cid+kind+str(now)).encode()).hexdigest()[:20]
        self.db.execute('INSERT OR IGNORE INTO outbox(event_id,condition_id,generation_ms,kind,text,created_ms,next_ms) VALUES (?,?,?,?,?,?,?)',
            (identity,cid,generation,kind,text+'\nEvent '+identity,now,now))

    def observe(self,target,issues,read_ok,now,reminder_ms,monitor_id):
        with self.transaction():
            current={r['code']:r for r in issues}
            for code,item in current.items():
                cid=target['id']+':'+code
                old=self.db.execute('SELECT * FROM conditions WHERE id=?',(cid,)).fetchone()
                if old is None or not old['active']:
                    self.db.execute('INSERT OR REPLACE INTO conditions VALUES (?,?,?,?,1,NULL,?,?)',
                        (cid,target['id'],code,now,item['detail'],item['severity']))
                    old=self.db.execute('SELECT * FROM conditions WHERE id=?',(cid,)).fetchone()
                if now-old['first_ms']>=item['delay_ms'] and (old['last_queued_ms'] is None or now-old['last_queued_ms']>=reminder_ms):
                    text=f"{item['severity']} | {monitor_id}\n{target['label']} | {code}\n{item['detail']}\nFirst UTC: {stamp(old['first_ms'])}\nObserved UTC: {stamp(now)}\nAlert saja; tidak ada tindakan/order otomatis."
                    self.queue(cid,'OPEN',text,now,old['first_ms'])
                    self.db.execute('UPDATE conditions SET last_queued_ms=? WHERE id=?',(now,cid))
            # Failed reads must never manufacture a recovery for conditions we cannot inspect.
            if read_ok:
                for old in self.db.execute('SELECT * FROM conditions WHERE target=? AND active=1',(target['id'],)).fetchall():
                    if old['code'] not in current:
                        self.db.execute('UPDATE conditions SET active=0 WHERE id=?',(old['id'],))
                        if old['last_queued_ms'] is not None:
                            self.queue(old['id'],'RECOVERY',f"RECOVERY | {monitor_id}\n{target['label']} | {old['code']}\nKondisi terpantau sudah pulih; bukan jaminan seluruh sistem bebas masalah.\nFirst UTC: {stamp(old['first_ms'])}\nRecovered UTC: {stamp(now)}",now,old['first_ms'])

    def deliver_one(self,http,token,chat,now):
        retry=self.db.execute("SELECT value FROM metadata WHERE key='retry_until_ms'").fetchone()
        if retry and now<int(retry[0]):return False
        with self.transaction():
            self.db.execute("""UPDATE outbox SET status='SUPERSEDED' WHERE status='PENDING' AND NOT EXISTS
                (SELECT 1 FROM conditions c WHERE c.id=outbox.condition_id AND c.first_ms=outbox.generation_ms
                 AND (outbox.kind='RECOVERY' OR c.active=1))""")
        row=self.db.execute("SELECT * FROM outbox WHERE status='PENDING' AND next_ms<=? ORDER BY CASE kind WHEN 'OPEN' THEN 0 ELSE 1 END,id LIMIT 1",(now,)).fetchone()
        if not row:return False
        try:message_id=send_telegram(http,token,chat,row['text'])
        except DeliveryError as e:
            with self.transaction():
                wait=max(e.retry_after,min(300,15*2**min(row['attempts'],5)))
                self.db.execute('UPDATE outbox SET attempts=attempts+1,next_ms=? WHERE id=?',(now+wait*1000,row['id']))
                self.set_meta('delivery_error',str(e))
                self.set_meta('retry_until_ms',now+wait*1000)
            return False
        with self.transaction():
            self.db.execute("UPDATE outbox SET status='SENT',delivered_ms=?,message_id=?,attempts=attempts+1 WHERE id=?",(now,message_id,row['id']))
            self.set_meta('delivery_error','');self.set_meta('last_delivered_ms',now)
            self.set_meta('retry_until_ms','0')
        return True

    def close(self):
        self.db.close()
        if self.writer_lock:self.writer_lock.close();self.writer_lock=None


def health(path,now):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        meta=dict(db.execute('SELECT key,value FROM metadata'))
        return meta.get('owner')=='ops_alerts' and 0<=now-int(meta.get('heartbeat_ms',0))<90000 and not meta.get('delivery_error') and not meta.get('watcher_error')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--database',default='/data/alerts.sqlite')
    p.add_argument('--action',choices=['run','once','health','status','test','discover'],default='status');a=p.parse_args()
    try:
        if a.action=='health':return 0 if health(a.database,int(time.time()*1000)) else 1
        if a.action=='status':
            with sqlite3.connect(Path(a.database).resolve().as_uri()+'?mode=ro',uri=True) as db:
                meta=dict(db.execute('SELECT key,value FROM metadata'))
                pending=db.execute("SELECT COUNT(*) FROM outbox WHERE status='PENDING'").fetchone()[0]
                active=db.execute('SELECT target,code FROM conditions WHERE active=1').fetchall()
            print(json.dumps(dict(metadata=meta,pending=pending,active=active)));return 0
        token,chat=credentials(require_chat=a.action!='discover');http=requests.Session();http.trust_env=False
        if a.action=='discover':
            try:print(json.dumps(discover(http,token)));return 0
            finally:http.close()
        if a.action=='test':
            try:
                mid=send_telegram(http,token,chat,'TEST | Golden Goose operational alerts\nBukan sinyal trading. Tidak ada order/perubahan akun.\nUTC: '+stamp(int(time.time()*1000)))
                print(json.dumps(dict(telegram_api_accepted=True,message_id=mid,phone_receipt_requires_confirmation=True)));return 0
            finally:http.close()
        config=load_config(a.config);store=AlertStore(a.database,config['monitor_id'])
        try:
            store.acquire_writer();stop=threading.Event()
            signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
            while not stop.is_set():
                now=int(time.time()*1000)
                try:
                    for target in config['targets']:
                        issues,valid=inspect_target(target,now)
                        store.observe(target,issues,valid,now,config['reminder_seconds']*1000,config['monitor_id'])
                    store.deliver_one(http,token,chat,now)
                    store.set_meta('watcher_error','')
                except Exception as e:
                    store.set_meta('watcher_error',type(e).__name__)
                    print(json.dumps(dict(watcher_error=type(e).__name__)),flush=True)
                store.set_meta('heartbeat_ms',int(time.time()*1000))
                if a.action=='once':return 0 if health(a.database,int(time.time()*1000)) else 2
                stop.wait(config['poll_seconds'])
        finally:store.close();http.close()
    except (sqlite3.Error,OSError,ValueError,DeliveryError) as e:
        print(json.dumps(dict(alert_service_error=str(e) if isinstance(e,DeliveryError) else type(e).__name__)),flush=True);return 2


if __name__=='__main__':raise SystemExit(main())

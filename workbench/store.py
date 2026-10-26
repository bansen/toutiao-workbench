import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .parsing import now_iso


class Store:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS materials (id TEXT PRIMARY KEY, content_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source_key TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, UNIQUE(task_id,source_key,version));
              CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def dump(value):
        return json.dumps(value,ensure_ascii=False)

    def materials(self):
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM materials ORDER BY rowid DESC')]

    def material(self,identity):
        with self.connection() as db:
            r=db.execute('SELECT payload FROM materials WHERE id=?',(identity,)).fetchone()
        if not r:
            raise KeyError('素材不存在')
        return json.loads(r[0])

    def upsert_material(self,item):
        with self.connection() as db:
            old=db.execute('SELECT id,payload FROM materials WHERE content_id=?',(item['content_id'],)).fetchone()
            identity=old['id'] if old else uuid.uuid4().hex
            merged={**(json.loads(old['payload']) if old else {}),**item,'id':identity}
            db.execute('INSERT INTO materials VALUES(?,?,?) ON CONFLICT(content_id) DO UPDATE SET payload=excluded.payload',
                       (identity,item['content_id'],self.dump(merged)))
        return merged, old is None

    def create_task(self,kind,options):
        task={'id':uuid.uuid4().hex,'kind':kind,'options':options,'status':'waiting','created_at':now_iso(),
              'checked':0,'accepted':0,'unknown':0,'errors':0,'generated':0,'new_ids':[],'logs':[]}
        with self.connection() as db:
            db.execute('INSERT INTO tasks VALUES(?,?)',(task['id'],self.dump(task)))
        return task

    def task(self,identity):
        with self.connection() as db:
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(identity,)).fetchone()
        if not row:
            raise KeyError('任务不存在')
        return json.loads(row[0])

    def tasks(self,limit=100):
        with self.connection() as db:
            query='SELECT payload FROM tasks ORDER BY rowid DESC'
            rows=db.execute(query+' LIMIT ?',(limit,)) if limit is not None else db.execute(query)
            return [json.loads(r[0]) for r in rows]

    def update_task(self,identity,**updates):
        with self.connection() as db:
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(identity,)).fetchone()
            if not row:
                raise KeyError('任务不存在')
            task=json.loads(row[0])
            task.update(updates,updated_at=now_iso())
            db.execute('UPDATE tasks SET payload=? WHERE id=?',(self.dump(task),identity))
        return task

    def log(self,identity,message):
        task=self.task(identity)
        self.update_task(identity,logs=(task['logs']+[f'{now_iso()}  {message}'])[-150:])

    def recover(self):
        with self.connection() as db:
            tasks=[json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks')]
        for task in tasks:
            if task['status'] in ('waiting','running','attention'):
                self.update_task(task['id'],status='failed')
                self.log(task['id'],'上次程序退出时任务未完成；未自动补跑，可手动重试。')

    def generated(self,task_id,source_key,version):
        with self.connection() as db:
            r=db.execute('SELECT payload FROM drafts WHERE task_id=? AND source_key=? AND version=?',(task_id,source_key,version)).fetchone()
        return json.loads(r[0]) if r else None

    def save_generated(self,task_id,source_key,version,payload):
        draft={**payload,'id':uuid.uuid4().hex,'task_id':task_id,'source_key':source_key,'version':version,'created_at':now_iso()}
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO drafts VALUES(?,?,?,?,?)',(draft['id'],task_id,source_key,version,self.dump(draft)))
        return self.generated(task_id,source_key,version)

    def drafts(self):
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM drafts ORDER BY rowid DESC')]

    def draft(self,identity):
        with self.connection() as db:
            row=db.execute('SELECT payload FROM drafts WHERE id=?',(identity,)).fetchone()
        if not row:
            raise KeyError('草稿不存在')
        return json.loads(row[0])

    def edit_draft(self,identity,title,body,verify):
        draft=self.draft(identity)
        draft.update(title=title,body=body,verify=verify,edited_at=now_iso())
        with self.connection() as db:
            db.execute('UPDATE drafts SET payload=? WHERE id=?',(self.dump(draft),identity))
        return draft

    def setting(self,key,default=None):
        with self.connection() as db:
            row=db.execute('SELECT payload FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self,key,value):
        with self.connection() as db:
            db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',(key,self.dump(value)))

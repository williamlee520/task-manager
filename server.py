from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            desc TEXT DEFAULT '',
            status TEXT DEFAULT '待办',
            priority TEXT DEFAULT '中',
            progress INTEGER DEFAULT 0,
            deadline TEXT DEFAULT '',
            created TEXT DEFAULT '',
            updated TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===== Models =====
class TaskCreate(BaseModel):
    name: str
    desc: str = ""
    status: str = "待办"
    priority: str = "中"
    progress: int = 0
    deadline: str = ""

class TaskUpdate(BaseModel):
    name: Optional[str] = None
    desc: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    progress: Optional[int] = None
    deadline: Optional[str] = None

# ===== API =====
@app.get("/api/tasks")
def list_tasks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY created DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/tasks")
def create_task(task: TaskCreate):
    import time, random
    tid = f"{int(time.time()):x}{random.randbytes(4).hex()}"
    today = time.strftime("%Y-%m-%d")
    conn = get_db()
    conn.execute(
        "INSERT INTO tasks (id,name,desc,status,priority,progress,deadline,created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, task.name, task.desc, task.status, task.priority, task.progress, task.deadline, today, today)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return dict(row)

@app.put("/api/tasks/{task_id}")
def update_task(task_id: str, task: TaskUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "任务不存在")
    import time
    today = time.strftime("%Y-%m-%d")
    fields = []
    vals = []
    for key in ["name","desc","status","priority","progress","deadline"]:
        val = getattr(task, key)
        if val is not None:
            fields.append(f"{key}=?")
            vals.append(val)
    if fields:
        fields.append("updated=?")
        vals.append(today)
        vals.append(task_id)
        conn.execute(f"UPDATE tasks SET {','.join(fields)} WHERE id=?", vals)
        conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row)

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    conn = get_db()
    existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "任务不存在")
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# ===== 静态文件 & SPA =====
app.mount("/static", StaticFiles(directory=os.path.dirname(__file__)), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))

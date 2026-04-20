from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import sqlite3
import os
import hashlib
import secrets
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tasks.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    # 密码表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL
        )
    """)
    # 任务表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            desc TEXT DEFAULT '',
            status TEXT DEFAULT '待办',
            priority TEXT DEFAULT '中',
            progress INTEGER DEFAULT 0,
            deadline TEXT DEFAULT '',
            is_draft INTEGER DEFAULT 0,
            created TEXT DEFAULT '',
            updated TEXT DEFAULT ''
        )
    """)
    # 阶段表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stages (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            name TEXT NOT NULL,
            completed INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===== 密码工具 =====
def hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return h, salt

def has_password() -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM auth LIMIT 1").fetchone()
    conn.close()
    return row is not None

def verify_password(password: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT password_hash, salt FROM auth WHERE id=1").fetchone()
    conn.close()
    if not row:
        return False
    h, _ = hash_password(password, row["salt"])
    return h == row["password_hash"]

def gen_session() -> str:
    return secrets.token_hex(32)

# 简易 session 存储（内存，重启失效需重新登录）
sessions = set()

# ===== Auth API =====
class SetPassword(BaseModel):
    password: str

class LoginPassword(BaseModel):
    password: str

@app.get("/api/auth/status")
def auth_status(request: Request):
    """返回是否已设置密码，以及当前请求是否已认证"""
    has_pwd = has_password()
    token = request.cookies.get("session_token", "")
    authenticated = token in sessions if has_pwd else True
    return {"has_password": has_pwd, "authenticated": authenticated}

@app.post("/api/auth/setup")
def setup_password(body: SetPassword, response: Response):
    if has_password():
        raise HTTPException(400, "密码已设置，不能重复设置")
    if len(body.password) < 4:
        raise HTTPException(400, "密码至少4位")
    h, salt = hash_password(body.password)
    conn = get_db()
    conn.execute("INSERT INTO auth (id, password_hash, salt) VALUES (1, ?, ?)", (h, salt))
    conn.commit()
    conn.close()
    token = gen_session()
    sessions.add(token)
    response = JSONResponse({"ok": True})
    response.set_cookie("session_token", token, max_age=86400*30, httponly=True, samesite="lax")
    return response

@app.post("/api/auth/login")
def login(body: LoginPassword, response: Response):
    if not verify_password(body.password):
        raise HTTPException(401, "密码错误")
    token = gen_session()
    sessions.add(token)
    response = JSONResponse({"ok": True})
    response.set_cookie("session_token", token, max_age=86400*30, httponly=True, samesite="lax")
    return response

@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get("session_token", "")
    sessions.discard(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie("session_token")
    return response

@app.post("/api/auth/change")
def change_password(request: Request, body: SetPassword, response: Response):
    token = request.cookies.get("session_token", "")
    if token not in sessions:
        raise HTTPException(401, "未登录")
    if len(body.password) < 4:
        raise HTTPException(400, "密码至少4位")
    h, salt = hash_password(body.password)
    conn = get_db()
    conn.execute("UPDATE auth SET password_hash=?, salt=? WHERE id=1", (h, salt))
    conn.commit()
    conn.close()
    # 重新生成 session
    sessions.discard(token)
    new_token = gen_session()
    sessions.add(new_token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session_token", new_token, max_age=86400*30, httponly=True, samesite="lax")
    return resp

# ===== 认证中间件（API 级别） =====
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # 不需要认证的路径
    public_paths = ["/", "/index.html", "/api/auth/status", "/api/auth/setup", "/api/auth/login"]
    if path in public_paths or path.startswith("/static") or path == "/favicon.ico":
        return await call_next(request)
    # 其他 API 需要认证
    if has_password():
        token = request.cookies.get("session_token", "")
        if token not in sessions:
            return JSONResponse({"detail": "未认证"}, status_code=401)
    return await call_next(request)

# ===== Task Models =====
class StageInput(BaseModel):
    name: str
    completed: bool = False

class TaskCreate(BaseModel):
    name: str
    desc: str = ""
    status: str = "待办"
    priority: str = "中"
    progress: int = 0
    deadline: str = ""
    is_draft: int = 0
    stages: List[StageInput] = []

class TaskUpdate(BaseModel):
    name: Optional[str] = None
    desc: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    progress: Optional[int] = None
    deadline: Optional[str] = None
    is_draft: Optional[int] = None
    stages: Optional[List[StageInput]] = None

# ===== Task API =====
@app.get("/api/tasks")
def list_tasks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY created DESC").fetchall()
    result = []
    for r in rows:
        task = dict(r)
        stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (task["id"],)).fetchall()
        task["stages"] = [dict(s) for s in stages]
        result.append(task)
    conn.close()
    return result

@app.post("/api/tasks")
def create_task(task: TaskCreate):
    import time, random
    tid = f"{int(time.time()):x}{random.randbytes(4).hex()}"
    today = time.strftime("%Y-%m-%d")

    # 自动计算进度
    progress = task.progress
    if task.stages:
        completed_count = sum(1 for s in task.stages if s.completed)
        progress = int(completed_count / len(task.stages) * 100) if task.stages else 0

    conn = get_db()
    conn.execute(
        "INSERT INTO tasks (id,name,desc,status,priority,progress,deadline,is_draft,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, task.name, task.desc, task.status, task.priority, progress, task.deadline, task.is_draft, today, today)
    )
    for i, s in enumerate(task.stages):
        sid = f"{int(time.time()):x}{random.randbytes(2).hex()}{i}"
        conn.execute("INSERT INTO stages (id,task_id,name,completed,sort_order) VALUES (?,?,?,?,?)",
                     (sid, tid, s.name, 1 if s.completed else 0, i))
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (tid,)).fetchall()
    conn.close()
    result = dict(row)
    result["stages"] = [dict(s) for s in stages]
    return result

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
    for key in ["name","desc","status","priority","deadline","is_draft"]:
        val = getattr(task, key)
        if val is not None:
            fields.append(f"{key}=?")
            vals.append(val)

    # 处理 stages 和 progress
    if task.stages is not None:
        # 删除旧阶段，插入新阶段
        conn.execute("DELETE FROM stages WHERE task_id=?", (task_id,))
        for i, s in enumerate(task.stages):
            sid = f"{int(time.time()):x}{secrets.token_hex(2)}{i}"
            conn.execute("INSERT INTO stages (id,task_id,name,completed,sort_order) VALUES (?,?,?,?,?)",
                         (sid, task_id, s.name, 1 if s.completed else 0, i))
        # 自动计算进度
        completed_count = sum(1 for s in task.stages if s.completed)
        progress = int(completed_count / len(task.stages) * 100) if task.stages else 0
        fields.append("progress=?")
        vals.append(progress)
    elif task.progress is not None:
        fields.append("progress=?")
        vals.append(task.progress)

    if fields:
        fields.append("updated=?")
        vals.append(today)
        vals.append(task_id)
        conn.execute(f"UPDATE tasks SET {','.join(fields)} WHERE id=?", vals)
        conn.commit()

    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (task_id,)).fetchall()
    conn.close()
    result = dict(row)
    result["stages"] = [dict(s) for s in stages]
    return result

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    conn = get_db()
    existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "任务不存在")
    conn.execute("DELETE FROM stages WHERE task_id=?", (task_id,))
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/tasks/{task_id}/duplicate")
def duplicate_task(task_id: str):
    conn = get_db()
    existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "任务不存在")
    stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (task_id,)).fetchall()

    import time, random
    new_id = f"{int(time.time()):x}{random.randbytes(4).hex()}"
    today = time.strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO tasks (id,name,desc,status,priority,progress,deadline,is_draft,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (new_id, existing["name"] + " (副本)", existing["desc"], "待办", existing["priority"],
         0, existing["deadline"], 0, today, today)
    )
    for i, s in enumerate(stages):
        sid = f"{int(time.time()):x}{random.randbytes(2).hex()}{i}"
        conn.execute("INSERT INTO stages (id,task_id,name,completed,sort_order) VALUES (?,?,?,?,?)",
                     (sid, new_id, s["name"], 0, i))
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (new_id,)).fetchone()
    new_stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (new_id,)).fetchall()
    conn.close()
    result = dict(row)
    result["stages"] = [dict(s) for s in new_stages]
    return result

# ===== 前端页面 =====
@app.get("/")
def serve_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

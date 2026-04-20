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
import subprocess
import threading
import time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "tasks.db")
BACKUP_PATH = os.path.join(DATA_DIR, "backup.json")

# GitHub 备份配置（通过环境变量传入）
GH_BACKUP_REPO = os.environ.get("GH_BACKUP_REPO", "")  # 如 williamlee520/task-manager
GH_BACKUP_TOKEN = os.environ.get("GH_BACKUP_TOKEN", "")  # GitHub PAT
GH_BACKUP_BRANCH = os.environ.get("GH_BACKUP_BRANCH", "data-backup")
BACKUP_INTERVAL = int(os.environ.get("BACKUP_INTERVAL", "300"))  # 秒

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL
        )
    """)
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

# ===== 启动时从 GitHub 恢复数据 =====
def restore_from_github():
    """服务启动时，尝试从 GitHub 仓库恢复 backup.json"""
    if not GH_BACKUP_REPO or not GH_BACKUP_TOKEN:
        print("[备份] 未配置 GH_BACKUP_REPO/GH_BACKUP_TOKEN，跳过恢复")
        return False
    try:
        url = f"https://api.github.com/repos/{GH_BACKUP_REPO}/contents/backup.json?ref={GH_BACKUP_BRANCH}"
        headers = {"Authorization": f"token {GH_BACKUP_TOKEN}", "Accept": "application/vnd.github.v3.raw"}
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        # 导入数据
        _import_backup(data)
        print(f"[备份] 从 GitHub 恢复成功，共 {len(data.get('tasks',[]))} 个任务")
        return True
    except Exception as e:
        print(f"[备份] 恢复失败: {e}")
        return False

def _import_backup(data):
    """将备份数据导入数据库"""
    conn = get_db()
    # 导入密码
    auth = data.get("auth")
    if auth:
        conn.execute("DELETE FROM auth")
        conn.execute("INSERT INTO auth (id, password_hash, salt) VALUES (1, ?, ?)",
                     (auth["password_hash"], auth["salt"]))
    # 导入任务和阶段
    for t in data.get("tasks", []):
        conn.execute("INSERT OR REPLACE INTO tasks (id,name,desc,status,priority,progress,deadline,is_draft,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (t["id"], t["name"], t.get("desc",""), t.get("status","待办"), t.get("priority","中"),
                      t.get("progress",0), t.get("deadline",""), t.get("is_draft",0), t.get("created",""), t.get("updated","")))
        for s in t.get("stages", []):
            conn.execute("INSERT OR REPLACE INTO stages (id,task_id,name,completed,sort_order) VALUES (?,?,?,?,?)",
                         (s["id"], t["id"], s["name"], s.get("completed",0), s.get("sort_order",0)))
    conn.commit()
    conn.close()

# ===== 数据导出为 JSON =====
def export_all_data():
    """将所有数据导出为 JSON"""
    conn = get_db()
    data = {"tasks": [], "auth": None, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    # 导出密码
    auth_row = conn.execute("SELECT * FROM auth WHERE id=1").fetchone()
    if auth_row:
        data["auth"] = {"password_hash": auth_row["password_hash"], "salt": auth_row["salt"]}
    # 导出任务
    tasks = conn.execute("SELECT * FROM tasks ORDER BY created DESC").fetchall()
    for t in tasks:
        task = dict(t)
        stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (task["id"],)).fetchall()
        task["stages"] = [dict(s) for s in stages]
        data["tasks"].append(task)
    conn.close()
    return data

# ===== 推送备份到 GitHub =====
def push_backup_to_github():
    """将 backup.json 推送到 GitHub 仓库的 data-backup 分支"""
    if not GH_BACKUP_REPO or not GH_BACKUP_TOKEN:
        return
    try:
        data = export_all_data()
        content = json.dumps(data, ensure_ascii=False, indent=2)
        import base64
        encoded = base64.b64encode(content.encode()).decode()

        headers = {"Authorization": f"token {GH_BACKUP_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        import urllib.request

        # 先获取文件 SHA（如果已存在）
        sha = None
        try:
            url = f"https://api.github.com/repos/{GH_BACKUP_REPO}/contents/backup.json?ref={GH_BACKUP_BRANCH}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                existing = json.loads(resp.read().decode())
                sha = existing.get("sha")
        except:
            pass

        # 创建或更新文件
        body = {
            "message": f"数据备份 {time.strftime('%Y-%m-%d %H:%M')}",
            "content": encoded,
            "branch": GH_BACKUP_BRANCH
        }
        if sha:
            body["sha"] = sha

        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_BACKUP_REPO}/contents/backup.json",
            data=json.dumps(body).encode(),
            headers=headers,
            method="PUT"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[备份] 推送到 GitHub 成功")
    except Exception as e:
        print(f"[备份] 推送失败: {e}")

# ===== 本地 JSON 备份 =====
def save_local_backup():
    """保存本地 JSON 备份"""
    try:
        data = export_all_data()
        with open(BACKUP_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[备份] 本地备份失败: {e}")

def load_local_backup():
    """从本地 JSON 恢复"""
    if not os.path.exists(BACKUP_PATH):
        return False
    try:
        with open(BACKUP_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _import_backup(data)
        print(f"[备份] 从本地备份恢复成功")
        return True
    except Exception as e:
        print(f"[备份] 本地恢复失败: {e}")
        return False

# ===== 定时备份线程 =====
backup_needed = False

def mark_backup_needed():
    global backup_needed
    backup_needed = True

def backup_loop():
    """后台线程：每 BACKUP_INTERVAL 秒检查是否需要备份"""
    global backup_needed
    while True:
        time.sleep(BACKUP_INTERVAL)
        if backup_needed:
            backup_needed = False
            save_local_backup()
            if GH_BACKUP_REPO and GH_BACKUP_TOKEN:
                push_backup_to_github()

# ===== 启动 =====
init_db()

# 先尝试从本地恢复，再尝试从 GitHub 恢复
if not load_local_backup():
    restore_from_github()

# 启动备份线程
backup_thread = threading.Thread(target=backup_loop, daemon=True)
backup_thread.start()

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

sessions = set()

# ===== Auth API =====
class SetPassword(BaseModel):
    password: str

class LoginPassword(BaseModel):
    password: str

@app.get("/api/auth/status")
def auth_status(request: Request):
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
    mark_backup_needed()
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
    mark_backup_needed()
    sessions.discard(token)
    new_token = gen_session()
    sessions.add(new_token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session_token", new_token, max_age=86400*30, httponly=True, samesite="lax")
    return resp

# ===== 认证中间件 =====
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public_paths = ["/", "/index.html", "/api/auth/status", "/api/auth/setup", "/api/auth/login"]
    if path in public_paths or path.startswith("/static") or path == "/favicon.ico":
        return await call_next(request)
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
    import time as _time, random as _random
    tid = f"{int(_time.time()):x}{_random.randbytes(4).hex()}"
    today = _time.strftime("%Y-%m-%d")

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
        sid = f"{int(_time.time()):x}{_random.randbytes(2).hex()}{i}"
        conn.execute("INSERT INTO stages (id,task_id,name,completed,sort_order) VALUES (?,?,?,?,?)",
                     (sid, tid, s.name, 1 if s.completed else 0, i))
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (tid,)).fetchall()
    conn.close()
    result = dict(row)
    result["stages"] = [dict(s) for s in stages]
    mark_backup_needed()
    return result

@app.put("/api/tasks/{task_id}")
def update_task(task_id: str, task: TaskUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "任务不存在")
    import time as _time
    today = _time.strftime("%Y-%m-%d")
    fields = []
    vals = []
    for key in ["name","desc","status","priority","deadline","is_draft"]:
        val = getattr(task, key)
        if val is not None:
            fields.append(f"{key}=?")
            vals.append(val)

    if task.stages is not None:
        conn.execute("DELETE FROM stages WHERE task_id=?", (task_id,))
        for i, s in enumerate(task.stages):
            sid = f"{int(_time.time()):x}{secrets.token_hex(2)}{i}"
            conn.execute("INSERT INTO stages (id,task_id,name,completed,sort_order) VALUES (?,?,?,?,?)",
                         (sid, task_id, s.name, 1 if s.completed else 0, i))
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
    mark_backup_needed()
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
    mark_backup_needed()
    return {"ok": True}

@app.post("/api/tasks/{task_id}/duplicate")
def duplicate_task(task_id: str):
    conn = get_db()
    existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "任务不存在")
    stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (task_id,)).fetchall()

    import time as _time, random as _random
    new_id = f"{int(_time.time()):x}{_random.randbytes(4).hex()}"
    today = _time.strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO tasks (id,name,desc,status,priority,progress,deadline,is_draft,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (new_id, existing["name"] + " (副本)", existing["desc"], "待办", existing["priority"],
         0, existing["deadline"], 0, today, today)
    )
    for i, s in enumerate(stages):
        sid = f"{int(_time.time()):x}{_random.randbytes(2).hex()}{i}"
        conn.execute("INSERT INTO stages (id,task_id,name,completed,sort_order) VALUES (?,?,?,?,?)",
                     (sid, new_id, s["name"], 0, i))
    conn.commit()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (new_id,)).fetchone()
    new_stages = conn.execute("SELECT * FROM stages WHERE task_id=? ORDER BY sort_order", (new_id,)).fetchall()
    conn.close()
    result = dict(row)
    result["stages"] = [dict(s) for s in new_stages]
    mark_backup_needed()
    return result

# ===== 手动备份/恢复 API =====
@app.get("/api/backup")
def get_backup(request: Request):
    """下载完整数据备份"""
    return export_all_data()

@app.post("/api/backup/restore")
def restore_backup(request: Request, data: dict):
    """从上传的 JSON 恢复数据"""
    _import_backup(data)
    mark_backup_needed()
    return {"ok": True, "tasks_count": len(data.get("tasks", []))}

# ===== 前端页面 =====
@app.get("/")
def serve_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

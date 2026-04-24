"""
任务管理器 - GitHub 作为数据存储
数据永远保存在 GitHub 仓库的 data-backup 分支的 data.json 中
服务重启、休眠都不会丢失数据
"""
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import os
import hashlib
import secrets
import json
import time
import threading
import base64
import urllib.request
import urllib.error

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# GitHub 存储配置
GH_REPO = os.environ.get("GH_BACKUP_REPO", "")
GH_TOKEN = os.environ.get("GH_BACKUP_TOKEN", "")
GH_BRANCH = os.environ.get("GH_BACKUP_BRANCH", "data-backup")
DATA_FILE = "data.json"

# 内存缓存 + 脏标记
_data_cache = None
_data_dirty = False
_data_lock = threading.Lock()

def _gh_api(method, path, data=None):
    """调用 GitHub API"""
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}?ref={GH_BRANCH}"
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def _gh_get_raw(path):
    """获取文件原始内容"""
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}?ref={GH_BRANCH}"
    headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3.raw"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def _gh_create_branch():
    """创建数据分支（如果不存在）"""
    # 获取默认分支的 SHA
    url = f"https://api.github.com/repos/{GH_REPO}"
    headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            repo = json.loads(resp.read().decode())
            default_branch = repo["default_branch"]
            default_sha = repo["default_branch"]  # 需要获取分支的 SHA

        # 获取默认分支的 commit SHA
        url2 = f"https://api.github.com/repos/{GH_REPO}/branches/{default_branch}"
        req2 = urllib.request.Request(url2, headers=headers)
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            branch_data = json.loads(resp2.read().decode())
            sha = branch_data["commit"]["sha"]

        # 创建数据分支
        create_url = f"https://api.github.com/repos/{GH_REPO}/git/refs"
        body = json.dumps({"ref": f"refs/heads/{GH_BRANCH}", "sha": sha}).encode()
        req3 = urllib.request.Request(create_url, data=body, headers={
            "Authorization": f"token {GH_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        }, method="POST")
        try:
            with urllib.request.urlopen(req3, timeout=10) as resp3:
                print(f"[存储] 创建分支 {GH_BRANCH} 成功")
        except urllib.error.HTTPError as e:
            if e.code == 422:  # 分支已存在
                pass
            else:
                raise
    except Exception as e:
        print(f"[存储] 创建分支失败: {e}")

def load_data():
    """从 GitHub 加载数据"""
    global _data_cache
    if not GH_REPO or not GH_TOKEN:
        print("[存储] 未配置 GitHub 存储，使用空数据")
        return {"tasks": [], "auth": None}

    try:
        content = _gh_get_raw(DATA_FILE)
        if content:
            data = json.loads(content)
            print(f"[存储] 从 GitHub 加载成功，{len(data.get('tasks',[]))} 个任务")
            _data_cache = data
            return data
    except Exception as e:
        print(f"[存储] 加载失败: {e}")

    return {"tasks": [], "auth": None}

def save_data(data):
    """保存数据到 GitHub"""
    if not GH_REPO or not GH_TOKEN:
        return False

    try:
        content = json.dumps(data, ensure_ascii=False, indent=2)
        encoded = base64.b64encode(content.encode()).decode()

        # 获取当前文件的 SHA
        file_info = _gh_api("GET", DATA_FILE)
        sha = file_info.get("sha") if file_info else None

        body = {
            "message": f"数据更新 {time.strftime('%Y-%m-%d %H:%M')}",
            "content": encoded,
            "branch": GH_BRANCH
        }
        if sha:
            body["sha"] = sha

        result = _gh_api("PUT", DATA_FILE, body)
        print(f"[存储] 保存到 GitHub 成功")
        return True
    except Exception as e:
        print(f"[存储] 保存到 GitHub 失败: {e}")
        return False

def get_data():
    """获取当前数据（内存缓存优先）"""
    global _data_cache
    with _data_lock:
        if _data_cache is None:
            _data_cache = load_data()
        return _data_cache

def mark_dirty():
    """标记数据已修改，立即保存到 GitHub"""
    global _data_dirty, _data_cache
    _data_dirty = True
    # 立即在后台线程中保存，不阻塞当前请求
    if _data_cache is not None:
        t = threading.Thread(target=lambda: save_data(_data_cache), daemon=True)
        t.start()

# 启动时创建分支并加载数据
_gh_create_branch()
_initial_data = load_data()
_data_cache = _initial_data

# ===== 密码工具 =====
def hash_password(password: str, salt: str = None) -> tuple:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return h, salt

def has_password() -> bool:
    data = get_data()
    return data.get("auth") is not None

def verify_password(password: str) -> bool:
    data = get_data()
    auth = data.get("auth")
    if not auth:
        return False
    h, _ = hash_password(password, auth["salt"])
    return h == auth["password_hash"]

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
        raise HTTPException(400, "密码已设置")
    if len(body.password) < 4:
        raise HTTPException(400, "密码至少4位")
    h, salt = hash_password(body.password)
    data = get_data()
    data["auth"] = {"password_hash": h, "salt": salt}
    mark_dirty()
    token = gen_session()
    sessions.add(token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session_token", token, max_age=86400*30, httponly=True, samesite="lax")
    return resp

@app.post("/api/auth/login")
def login(body: LoginPassword, response: Response):
    if not verify_password(body.password):
        raise HTTPException(401, "密码错误")
    token = gen_session()
    sessions.add(token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session_token", token, max_age=86400*30, httponly=True, samesite="lax")
    return resp

@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get("session_token", "")
    sessions.discard(token)
    return JSONResponse({"ok": True})

@app.post("/api/auth/change")
def change_password(request: Request, body: SetPassword, response: Response):
    token = request.cookies.get("session_token", "")
    if token not in sessions:
        raise HTTPException(401, "未登录")
    if len(body.password) < 4:
        raise HTTPException(400, "密码至少4位")
    h, salt = hash_password(body.password)
    data = get_data()
    data["auth"] = {"password_hash": h, "salt": salt}
    mark_dirty()
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
    public_paths = ["/", "/index.html", "/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/debug"]
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
    data = get_data()
    return data.get("tasks", [])

@app.post("/api/tasks")
def create_task(task: TaskCreate):
    tid = f"{int(time.time()):x}{secrets.token_hex(6)}"
    today = time.strftime("%Y-%m-%d")

    progress = task.progress
    stages_data = []
    if task.stages:
        for i, s in enumerate(task.stages):
            stages_data.append({
                "id": f"{int(time.time()):x}{secrets.token_hex(3)}{i}",
                "task_id": tid,
                "name": s.name,
                "completed": s.completed,
                "sort_order": i
            })
        completed_count = sum(1 for s in task.stages if s.completed)
        progress = int(completed_count / len(task.stages) * 100) if task.stages else 0

    new_task = {
        "id": tid, "name": task.name, "desc": task.desc,
        "status": task.status, "priority": task.priority,
        "progress": progress, "deadline": task.deadline,
        "is_draft": task.is_draft, "created": today, "updated": today,
        "stages": stages_data
    }

    data = get_data()
    data["tasks"].insert(0, new_task)
    mark_dirty()
    return new_task

@app.put("/api/tasks/{task_id}")
def update_task(task_id: str, task: TaskUpdate):
    data = get_data()
    idx = None
    for i, t in enumerate(data["tasks"]):
        if t["id"] == task_id:
            idx = i
            break
    if idx is None:
        raise HTTPException(404, "任务不存在")

    today = time.strftime("%Y-%m-%d")
    t = data["tasks"][idx]

    for key in ["name","desc","status","priority","deadline","is_draft"]:
        val = getattr(task, key)
        if val is not None:
            t[key] = val

    if task.stages is not None:
        stages_data = []
        for i, s in enumerate(task.stages):
            stages_data.append({
                "id": f"{int(time.time()):x}{secrets.token_hex(3)}{i}",
                "task_id": task_id,
                "name": s.name,
                "completed": s.completed,
                "sort_order": i
            })
        t["stages"] = stages_data
        completed_count = sum(1 for s in task.stages if s.completed)
        t["progress"] = int(completed_count / len(task.stages) * 100) if task.stages else 0
    elif task.progress is not None:
        t["progress"] = task.progress

    t["updated"] = today
    mark_dirty()
    return t

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    data = get_data()
    data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]
    mark_dirty()
    return {"ok": True}

@app.post("/api/tasks/{task_id}/duplicate")
def duplicate_task(task_id: str):
    data = get_data()
    source = next((t for t in data["tasks"] if t["id"] == task_id), None)
    if not source:
        raise HTTPException(404, "任务不存在")

    new_id = f"{int(time.time()):x}{secrets.token_hex(6)}"
    today = time.strftime("%Y-%m-%d")
    new_task = {
        "id": new_id,
        "name": source["name"] + " (副本)",
        "desc": source.get("desc", ""),
        "status": "待办",
        "priority": source.get("priority", "中"),
        "progress": 0,
        "deadline": source.get("deadline", ""),
        "is_draft": 0,
        "created": today,
        "updated": today,
        "stages": [
            {**s, "id": f"{int(time.time()):x}{secrets.token_hex(3)}{i}", "task_id": new_id, "completed": False}
            for i, s in enumerate(source.get("stages", []))
        ]
    }
    data["tasks"].insert(0, new_task)
    mark_dirty()
    return new_task

# ===== 手动备份/恢复 =====
@app.get("/api/backup")
def get_backup():
    return get_data()

@app.post("/api/backup/restore")
def restore_backup(payload: dict):
    global _data_cache
    with _data_lock:
        _data_cache = payload
        mark_dirty()
    return {"ok": True, "tasks_count": len(payload.get("tasks", []))}

# ===== 强制立即保存 =====
@app.post("/api/force-save")
def force_save():
    data = get_data()
    ok = save_data(data)
    return {"ok": ok}

# ===== 诊断接口 =====
@app.get("/api/debug")
def debug_info(request: Request):
    """诊断 GitHub 存储配置是否正确"""
    token_preview = GH_TOKEN[:6] + "..." if GH_TOKEN else "(空)"
    return {
        "GH_REPO": GH_REPO or "(空)",
        "GH_TOKEN_preview": token_preview,
        "GH_BRANCH": GH_BRANCH,
        "data_loaded": _data_cache is not None,
        "tasks_count": len(_data_cache.get("tasks",[])) if _data_cache else 0,
    }

# ===== 前端 =====
@app.get("/")
def serve_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

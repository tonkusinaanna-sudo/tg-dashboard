import json
import os
import sqlite3
from datetime import datetime
import anthropic
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import pathlib
import uvicorn

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = pathlib.Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DB_PATH = str(BASE_DIR / "dashboard.db")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT, uploaded_at TEXT, message_count INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS agreements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT, date TEXT, partner TEXT,
        description TEXT, deadline TEXT, status TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT, date TEXT, description TEXT,
        priority TEXT, status TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS partners (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT, name TEXT, partner_type TEXT,
        last_contact TEXT, status TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT, partner TEXT, method TEXT,
        commission TEXT, daily_limit TEXT, reserve TEXT, created_at TEXT)""")
    conn.commit()
    conn.close()

init_db()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def extract_messages(data: dict, max_messages: int = 500) -> str:
    messages = data.get("messages", [])
    lines = []
    for msg in messages[-max_messages:]:
        if msg.get("type") != "message":
            continue
        date = msg.get("date", "")[:10]
        from_name = msg.get("from", "Unknown")
        text = msg.get("text", "")
        if isinstance(text, list):
            text = " ".join(
                part if isinstance(part, str) else part.get("text", "")
                for part in text
            )
        if text.strip():
            lines.append(f"[{date}] {from_name}: {text}")
    return "\n".join(lines)


def analyze_with_claude(chat_name: str, messages_text: str) -> dict:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY не задан")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Ты аналитик платёжного процессинга (High Risk: казино, трейдеры, платёжные системы).
Проанализируй переписку из чата "{chat_name}" и извлеки структурированную информацию.

ПЕРЕПИСКА:
{messages_text[:40000]}

Верни ТОЛЬКО валидный JSON без пояснений:
{{
  "agreements": [
    {{"partner": "имя", "description": "суть", "deadline": "дедлайн или null", "status": "Принято|Ожидание|Требует действий|Отклонено"}}
  ],
  "incidents": [
    {{"description": "описание", "priority": "Критично|Высокий|Средний|Низкий", "status": "Открыто|В работе|Решено"}}
  ],
  "partners": [
    {{"name": "имя", "type": "Казино|Трейдер|Платёжная система|Банк|Другое", "last_contact": "описание", "status": "Активный|Переговоры|Мониторинг|Неактивный"}}
  ],
  "limits": [
    {{"partner": "имя", "method": "метод", "commission": "% или null", "daily_limit": "лимит или null", "reserve": "условия или null"}}
  ]
}}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


@app.post("/api/upload")
async def upload_chat(file: UploadFile = File(...)):
    if not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="Только JSON файлы")
    content = await file.read()
    try:
        data = json.loads(content)
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON")

    chat_name = data.get("name") or file.filename.replace(".json", "")
    messages_text = extract_messages(data)
    message_count = len([m for m in data.get("messages", []) if m.get("type") == "message"])

    if not messages_text.strip():
        raise HTTPException(status_code=400, detail="Нет сообщений в файле")

    try:
        result = analyze_with_claude(chat_name, messages_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Ошибка анализа, попробуйте ещё раз")

    conn = get_db()
    c = conn.cursor()
    now = datetime.now().isoformat()

    c.execute("INSERT INTO chats (chat_name, uploaded_at, message_count) VALUES (?,?,?)",
              (chat_name, now, message_count))
    for a in result.get("agreements", []):
        c.execute("INSERT INTO agreements (chat_name,date,partner,description,deadline,status,created_at) VALUES (?,?,?,?,?,?,?)",
                  (chat_name, now[:10], a.get("partner",""), a.get("description",""), a.get("deadline"), a.get("status","Ожидание"), now))
    for i in result.get("incidents", []):
        c.execute("INSERT INTO incidents (chat_name,date,description,priority,status,created_at) VALUES (?,?,?,?,?,?)",
                  (chat_name, now[:10], i.get("description",""), i.get("priority","Средний"), i.get("status","Открыто"), now))
    for p in result.get("partners", []):
        c.execute("INSERT INTO partners (chat_name,name,partner_type,last_contact,status,created_at) VALUES (?,?,?,?,?,?)",
                  (chat_name, p.get("name",""), p.get("type","Другое"), p.get("last_contact",""), p.get("status","Активный"), now))
    for l in result.get("limits", []):
        c.execute("INSERT INTO limits (chat_name,partner,method,commission,daily_limit,reserve,created_at) VALUES (?,?,?,?,?,?,?)",
                  (chat_name, l.get("partner",""), l.get("method",""), l.get("commission"), l.get("daily_limit"), l.get("reserve"), now))

    conn.commit()
    conn.close()

    return {"success": True, "chat_name": chat_name, "message_count": message_count,
            "extracted": {"agreements": len(result.get("agreements",[])),
                          "incidents": len(result.get("incidents",[])),
                          "partners": len(result.get("partners",[])),
                          "limits": len(result.get("limits",[]))}}


@app.get("/api/dashboard")
def get_dashboard():
    conn = get_db()
    c = conn.cursor()
    chats = [dict(r) for r in c.execute("SELECT * FROM chats ORDER BY uploaded_at DESC").fetchall()]
    agreements = [dict(r) for r in c.execute("SELECT * FROM agreements ORDER BY created_at DESC").fetchall()]
    incidents = [dict(r) for r in c.execute("SELECT * FROM incidents ORDER BY created_at DESC").fetchall()]
    partners = [dict(r) for r in c.execute("SELECT * FROM partners ORDER BY created_at DESC").fetchall()]
    limits = [dict(r) for r in c.execute("SELECT * FROM limits ORDER BY created_at DESC").fetchall()]
    conn.close()
    return {
        "stats": {
            "chats": len(chats),
            "agreements": len(agreements),
            "open_incidents": len([i for i in incidents if i["status"] != "Решено"]),
            "critical_incidents": len([i for i in incidents if i["priority"] == "Критично" and i["status"] != "Решено"]),
            "partners": len(partners),
        },
        "chats": chats, "agreements": agreements,
        "incidents": incidents, "partners": partners, "limits": limits,
    }


@app.delete("/api/clear")
def clear_all():
    conn = get_db()
    c = conn.cursor()
    for table in ["chats", "agreements", "incidents", "partners", "limits"]:
        c.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    return {"success": True}


app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/")
def serve_index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)

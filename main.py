import sqlite3
import uuid
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Body, Header, Depends
from pydantic import BaseModel
import time
import os

app = FastAPI()

# --- Configuration & Security ---
ANKIPLACE_SECRET = os.getenv("ANKIPLACE_SECRET", "change-me-please")

# Simple in-memory rate limiting: {user_id: last_request_time}
RATE_LIMIT_COOLDOWN = 1.0 # seconds
user_last_request = {}

async def verify_secret(x_ankiplace_secret: str = Header(None)):
    if x_ankiplace_secret != ANKIPLACE_SECRET:
        raise HTTPException(status_code=403, detail="Invalid or missing secret key")

def check_rate_limit(user_id: str):
    now = time.time()
    last_time = user_last_request.get(user_id, 0)
    if now - last_time < RATE_LIMIT_COOLDOWN:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait.")
    user_last_request[user_id] = now

# --- Database ---
DB_FILE = os.getenv("DB_PATH", "canvas.db") # Default to local for dev, override for Docker

# Ensure directory exists if path is provided
db_dir = os.path.dirname(DB_FILE)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # Canvas table
    c.execute('''
        CREATE TABLE IF NOT EXISTS canvas (
            x INTEGER,
            y INTEGER,
            color INTEGER,
            last_user_id TEXT,
            last_modified REAL,
            PRIMARY KEY (x, y)
        )
    ''')
    # Users table - added paint_balance
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            paint_balance INTEGER DEFAULT 0,
            created_at REAL
        )
    ''')
    # Review proofs table [NEW]
    c.execute('''
        CREATE TABLE IF NOT EXISTS review_proofs (
            user_id TEXT,
            card_id INTEGER,
            timestamp REAL,
            PRIMARY KEY (user_id, card_id, timestamp)
        )
    ''')
    
    # Initialize canvas if empty
    c.execute('SELECT count(*) FROM canvas')
    if c.fetchone()[0] == 0:
        for x in range(32):
            for y in range(32):
                c.execute('INSERT INTO canvas (x, y, color, last_user_id, last_modified) VALUES (?, ?, ?, ?, ?)',
                          (x, y, 0, None, 0))
    
    conn.commit()
    conn.close()

init_db()

# --- Models ---
class PixelUpdate(BaseModel):
    x: int
    y: int
    color: int
    user_id: str

class UserRegister(BaseModel):
    username: str

class ReviewProof(BaseModel):
    card_id: int
    timestamp: float

class ReviewSubmission(BaseModel):
    user_id: str
    proofs: List[ReviewProof]

class CanvasPixel(BaseModel):
    x: int
    y: int
    color: int
    last_user_id: Optional[str] = None
    last_modified: Optional[float] = None # Timestamp

# --- Routes ---

@app.get("/canvas")
def get_canvas():
    """Returns the entire 32x32 canvas state."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT x, y, color FROM canvas')
    rows = c.fetchall()
    conn.close()
    
    # Convert to a simple 2D array or list of objects
    # For bandwidth efficiency, let's return a flat list of integers (32*32 = 1024 ints)
    # The client can reconstruct the grid.
    # Order: row by row (y=0, x=0..31; y=1, x=0..31)
    grid = [0] * (32 * 32)
    for row in rows:
        idx = row['y'] * 32 + row['x']
        if 0 <= idx < 1024:
            grid[idx] = row['color']
            
    return {"canvas": grid}

@app.get("/pixel/{x}/{y}")
def get_pixel_details(x: int, y: int):
    """Returns details about a specific pixel (who painted it, when)."""
    if not (0 <= x < 32 and 0 <= y < 32):
        raise HTTPException(status_code=400, detail="Coordinates out of bounds")
        
    conn = get_db_connection()
    c = conn.cursor()
    # Join with users table to get username
    c.execute('''
        SELECT canvas.*, users.username 
        FROM canvas 
        LEFT JOIN users ON canvas.last_user_id = users.user_id
        WHERE x = ? AND y = ?
    ''', (x, y))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {
            "x": row['x'],
            "y": row['y'],
            "color": row['color'],
            "last_user_id": row['last_user_id'],
            "username": row['username'], # Added field
            "last_modified": row['last_modified']
        }
    else:
        return {"error": "Pixel not found"}

@app.post("/paint")
def paint_pixel(pixel: PixelUpdate):
    """Updates a pixel's color."""
    if not (0 <= pixel.x < 32 and 0 <= pixel.y < 32):
        raise HTTPException(status_code=400, detail="Coordinates out of bounds")
    if not (0 <= pixel.color < 16): # Assuming 16 colors
         raise HTTPException(status_code=400, detail="Invalid color index (0-15)")

    conn = get_db_connection()
    c = conn.cursor()
    
    # Optional: Verify user_id exists? For now, we trust the client or auto-create?
    # Let's enforce user existence for better tracking
    c.execute('SELECT paint_balance FROM users WHERE user_id = ?', (pixel.user_id,))
    row = c.fetchone()
    if not row:
         conn.close()
         raise HTTPException(status_code=404, detail="User ID not found. Register first.")
    
    if row['paint_balance'] < 1:
        conn.close()
        raise HTTPException(status_code=403, detail="Not enough paint drops. Study more cards!")

    timestamp = time.time()
    c.execute('''
        UPDATE canvas 
        SET color = ?, last_user_id = ?, last_modified = ?
        WHERE x = ? AND y = ?
    ''', (pixel.color, pixel.user_id, timestamp, pixel.x, pixel.y))
    
    # Deduct 1 paint
    c.execute('UPDATE users SET paint_balance = paint_balance - 1 WHERE user_id = ?', (pixel.user_id,))
    
    conn.commit()
    conn.close()
    return {"status": "success", "x": pixel.x, "y": pixel.y, "color": pixel.color}

@app.post("/user")
def register_user(user: UserRegister):
    """Registers a new user and returns a User ID."""
    new_id = str(uuid.uuid4())
    timestamp = time.time()
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO users (user_id, username, created_at) VALUES (?, ?, ?)',
              (new_id, user.username, timestamp))
    conn.commit()
    conn.close()
    
    return {"user_id": new_id, "username": user.username}

@app.post("/submit-reviews", dependencies=[Depends(verify_secret)])
def submit_reviews(submission: ReviewSubmission):
    """Processes review proofs and awards paint."""
    conn = get_db_connection()
    c = conn.cursor()
    
    # Verify user exists
    c.execute('SELECT paint_balance FROM users WHERE user_id = ?', (submission.user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    
    current_balance = row['paint_balance']
    new_proofs_count = 0
    
    for proof in submission.proofs:
        # Check if this proof was already submitted
        c.execute('SELECT 1 FROM review_proofs WHERE user_id = ? AND card_id = ? AND timestamp = ?',
                  (submission.user_id, proof.card_id, proof.timestamp))
        if not c.fetchone():
            # New proof!
            c.execute('INSERT INTO review_proofs (user_id, card_id, timestamp) VALUES (?, ?, ?)',
                      (submission.user_id, proof.card_id, proof.timestamp))
            new_proofs_count += 1
    
    # Rule: 10 reviews = 1 paint
    # We might need to store "remainder" reviews in user table, or just calculate from total proofs.
    # For now, let's just award 1 paint per 10 NEW proofs found in this batch.
    # A more robust way: total_proofs / 10 - total_paint_ever_awarded
    paint_awarded = new_proofs_count // 10
    
    if paint_awarded > 0:
        c.execute('UPDATE users SET paint_balance = paint_balance + ? WHERE user_id = ?',
                  (paint_awarded, submission.user_id))
    
    conn.commit()
    conn.close()
    
    return {"status": "success", "new_proofs": new_proofs_count, "paint_awarded": paint_awarded}

@app.get("/user/{user_id}/balance")
def get_balance(user_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT paint_balance FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {"user_id": user_id, "paint_balance": row['paint_balance']}
    else:
        raise HTTPException(status_code=404, detail="User not found")

@app.get("/user/{user_id}")
def get_user(user_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT username, created_at FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {"user_id": user_id, "username": row['username'], "created_at": row['created_at']}
    else:
        raise HTTPException(status_code=404, detail="User not found")

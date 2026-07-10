from __future__ import annotations

import time
from typing import Optional

from fastapi import Cookie, HTTPException, Response

from .database import db
from .security import SESSION_COOKIE, hash_password, new_token, verify_password


SESSION_TTL = 60 * 60 * 24 * 14


def create_user(username: str, password: str) -> int:
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash) VALUES (?, ?)",
                (username.strip(), hash_password(password)),
            )
            user_id = int(cur.lastrowid)
            conn.execute("INSERT INTO user_settings(user_id) VALUES (?)", (user_id,))
            return user_id
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(status_code=409, detail="用户名已存在") from exc
            raise


def login_user(username: str, password: str, response: Response) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = new_token()
        expires = int(time.time()) + SESSION_TTL
        conn.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES (?, ?, ?)", (token, row["id"], expires))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return {"id": row["id"], "username": row["username"]}


def logout_user(session_token: Optional[str], response: Response) -> None:
    if session_token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (session_token,))
    response.delete_cookie(SESSION_COOKIE)


def current_user(session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)) -> dict:
    if not session_token:
        raise HTTPException(status_code=401, detail="未登录")
    with db() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.username
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token=? AND sessions.expires_at>?
            """,
            (session_token, int(time.time())),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="登录已过期")
    return {"id": row["id"], "username": row["username"]}


def optional_user(session_token: Optional[str]) -> Optional[dict]:
    if not session_token:
        return None
    with db() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.username
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token=? AND sessions.expires_at>?
            """,
            (session_token, int(time.time())),
        ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "username": row["username"]}

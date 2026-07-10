from __future__ import annotations

import os

from .database import db
from .models import SettingsIn, SettingsOut
from .security import decrypt_secret, encrypt_secret


def get_settings(user_id: int, include_secret: bool = False) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        raise RuntimeError("missing settings row")
    api_key = decrypt_secret(row["deepseek_api_key_enc"]) if include_secret else None
    return {
        "deepseek_api_key": api_key,
        "deepseek_api_key_set": bool(row["deepseek_api_key_enc"]),
        "deepseek_base_url": row["deepseek_base_url"],
        "deepseek_model": row["deepseek_model"],
        "seed": row["seed"],
        "version": row["version"],
        "center_x": row["center_x"],
        "center_z": row["center_z"],
        "search_radius": row["search_radius"],
        "max_results": row["max_results"] or 1,
    }


def settings_out(user_id: int) -> SettingsOut:
    s = get_settings(user_id)
    return SettingsOut(
        **s,
        key_storage="Fernet 加密保存" if os.getenv("APP_ENCRYPTION_KEY") else "Fernet 开发密钥加密保存；请在生产环境设置 APP_ENCRYPTION_KEY",
    )


def save_settings(user_id: int, payload: SettingsIn) -> SettingsOut:
    current = get_settings(user_id, include_secret=True)
    api_key_enc = encrypt_secret(payload.deepseek_api_key) if payload.deepseek_api_key is not None else encrypt_secret(current.get("deepseek_api_key"))
    with db() as conn:
        conn.execute(
            """
            UPDATE user_settings SET
                deepseek_api_key_enc=?,
                deepseek_base_url=?,
                deepseek_model=?,
                seed=?,
                version=?,
                center_x=?,
                center_z=?,
                search_radius=?,
                max_results=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE user_id=?
            """,
            (
                api_key_enc,
                payload.deepseek_base_url,
                payload.deepseek_model,
                payload.seed,
                payload.version,
                payload.center_x,
                payload.center_z,
                payload.search_radius,
                payload.max_results,
                user_id,
            ),
        )
    return settings_out(user_id)

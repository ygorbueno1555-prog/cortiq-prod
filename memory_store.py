"""Persistent memory for decisions/theses.

Stores each completed analysis so we can track thesis drift over time.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "decision_memory.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                target TEXT NOT NULL,
                verdict TEXT,
                confidence TEXT,
                thesis_input TEXT,
                mandate_input TEXT,
                report_markdown TEXT NOT NULL,
                query_count INTEGER NOT NULL DEFAULT 0,
                source_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mode_target_created ON analyses(mode, target, created_at DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def save_analysis(
    *,
    mode: str,
    target: str,
    report_markdown: str,
    verdict: str = "",
    confidence: str = "",
    thesis_input: str = "",
    mandate_input: str = "",
    query_count: int = 0,
    source_count: int = 0,
) -> None:
    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO analyses (
                mode, target, verdict, confidence,
                thesis_input, mandate_input, report_markdown,
                query_count, source_count, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mode,
                target.upper() if mode == "equity" else target,
                verdict,
                confidence,
                thesis_input,
                mandate_input,
                report_markdown,
                int(query_count),
                int(source_count),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent(limit: int = 20, mode: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        if mode:
            rows = conn.execute(
                """
                SELECT id, mode, target, verdict, confidence, query_count, source_count, created_at
                FROM analyses
                WHERE mode = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (mode, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, mode, target, verdict, confidence, query_count, source_count, created_at
                FROM analyses
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_latest(mode: str, target: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM analyses
            WHERE mode = ? AND target = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (mode, target.upper() if mode == "equity" else target),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

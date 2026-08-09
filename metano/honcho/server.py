"""Honcho FastAPI service for dialectic user modeling."""

import json
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .models import (
    init_honcho_db, get_honcho_db, create_user, get_user, get_profile,
    add_observation, get_observations, get_beliefs, delete_belief,
)
from .dialectic import dialectic_reason, extract_observations, compress_beliefs

app = FastAPI(title="metano-honcho")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ObservationInput(BaseModel):
    content: str
    category: str = "general"
    session_id: str = ""


class DialecticInput(BaseModel):
    content: str
    category: str = "general"


class ConversationInput(BaseModel):
    text: str


@app.on_event("startup")
def startup():
    init_honcho_db()


@app.post("/api/users")
def api_create_user(name: str = "user", id: str = "default"):
    conn = get_honcho_db()
    try:
        return create_user(conn, name=name, user_id=id)
    finally:
        conn.close()


@app.get("/api/users/{user_id}")
def api_get_user(user_id: str):
    conn = get_honcho_db()
    try:
        user = get_user(conn, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        return user
    finally:
        conn.close()


@app.get("/api/users/{user_id}/profile")
def api_get_profile(user_id: str):
    conn = get_honcho_db()
    try:
        profile = get_profile(conn, user_id)
        if not profile:
            raise HTTPException(404, "User not found")
        return profile
    finally:
        conn.close()


@app.post("/api/users/{user_id}/observations")
def api_add_observation(user_id: str, body: ObservationInput):
    conn = get_honcho_db()
    try:
        if not get_user(conn, user_id):
            create_user(conn, user_id=user_id)
        return add_observation(conn, user_id, body.content, body.category, body.session_id)
    finally:
        conn.close()


@app.get("/api/users/{user_id}/observations")
def api_get_observations(user_id: str, limit: int = 50):
    conn = get_honcho_db()
    try:
        return get_observations(conn, user_id, limit)
    finally:
        conn.close()


@app.post("/api/users/{user_id}/dialectic")
def api_dialectic(user_id: str, body: DialecticInput):
    conn = get_honcho_db()
    try:
        if not get_user(conn, user_id):
            create_user(conn, user_id=user_id)
        # Record the observation first
        add_observation(conn, user_id, body.content, body.category)
    finally:
        conn.close()
    # Run dialectic reasoning (manages its own connection)
    result = dialectic_reason(user_id, body.content, body.category)
    return result


@app.post("/api/users/{user_id}/extract")
def api_extract(user_id: str, body: ConversationInput):
    conn = get_honcho_db()
    try:
        if not get_user(conn, user_id):
            create_user(conn, user_id=user_id)
        observations = extract_observations(user_id, body.text)
        # Auto-run dialectic for each observation
        results = []
        for obs in observations:
            add_observation(conn, user_id, obs.get("content", ""), obs.get("category", "general"))
            result = dialectic_reason(user_id, obs.get("content", ""), obs.get("category", "general"))
            results.append(result)
        return {"observations": observations, "dialectic_results": results}
    finally:
        conn.close()


@app.get("/api/users/{user_id}/beliefs")
def api_get_beliefs(user_id: str):
    conn = get_honcho_db()
    try:
        return get_beliefs(conn, user_id)
    finally:
        conn.close()


@app.delete("/api/users/{user_id}/beliefs/{belief_id}")
def api_delete_belief(user_id: str, belief_id: str):
    conn = get_honcho_db()
    try:
        if not delete_belief(conn, belief_id):
            raise HTTPException(404, "Belief not found")
        return {"deleted": belief_id}
    finally:
        conn.close()


@app.post("/api/users/{user_id}/compress")
def api_compress(user_id: str):
    return compress_beliefs(user_id)

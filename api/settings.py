from fastapi import APIRouter
import redis
import os

router = APIRouter()

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
r = redis.from_url(REDIS_URL)

# Defaults
DEFAULT_IDLE_TIMEOUT = 1800
DEFAULT_STALE_TICKS = 15

@router.get("/settings")
def get_settings():
    cfg = r.hgetall("agent_config")
    idle = int(cfg.get(b"IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT))
    stale = int(cfg.get(b"STALE_TICKS", DEFAULT_STALE_TICKS))
    return {"IDLE_TIMEOUT": idle, "STALE_TICKS": stale}

@router.post("/settings")
def update_settings(idle_timeout: int, stale_ticks: int):
    r.hset("agent_config", mapping={
        "IDLE_TIMEOUT": idle_timeout,
        "STALE_TICKS": stale_ticks,
    })
    return {"success": True, "IDLE_TIMEOUT": idle_timeout, "STALE_TICKS": stale_ticks}

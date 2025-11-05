# performance_config.py
import os

# Performance tuning settings
UVICORN_CONFIG = {
    "workers": int(os.getenv("UVICORN_WORKERS", 1)),
    "loop": "asyncio",
    "http": "httptools", 
    "limit_max_requests": 1000,
    "timeout_keep_alive": 5,
    "max_requests": 1000,
    "max_requests_jitter": 100,
}

# Cache configuration
CACHE_CONFIG = {
    "max_size": 1000,
    "ttl": 3600,
}

# Rate limiting (optional)
RATE_LIMIT_CONFIG = {
    "requests_per_minute": 60,
}

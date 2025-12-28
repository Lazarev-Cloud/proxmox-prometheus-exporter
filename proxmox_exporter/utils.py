import importlib.util
import subprocess
import sys
import time
from collections import deque
from functools import wraps

from .config import logger


def ensure_psutil():
    if importlib.util.find_spec('psutil') is None:
        print('psutil not installed. Installing...')
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'psutil'], check=True)
    import psutil
    return psutil


class MetricCache:
    """Simple TTL cache for expensive operations"""
    def __init__(self, default_ttl=60):
        self.cache = {}
        self.default_ttl = default_ttl

    def get(self, key, compute_func, ttl=None):
        """Get value from cache or compute it"""
        if ttl is None:
            ttl = self.default_ttl

        now = time.time()
        if key in self.cache:
            value, expiry = self.cache[key]
            if now < expiry:
                return value

        value = compute_func()
        self.cache[key] = (value, now + ttl)
        return value

    def clear_expired(self):
        """Remove expired entries"""
        now = time.time()
        expired = [k for k, (_, exp) in self.cache.items() if now >= exp]
        for k in expired:
            del self.cache[k]


class RateLimiter:
    """Rate limiter for expensive operations"""
    def __init__(self, max_calls=10, period=60):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    def allow(self):
        """Check if operation is allowed"""
        now = time.time()
        while self.calls and self.calls[0] <= now - self.period:
            self.calls.popleft()

        if len(self.calls) < self.max_calls:
            self.calls.append(now)
            return True
        return False


def timed_operation(timeout=5):
    """Decorator to add timeout to operations"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except subprocess.TimeoutExpired:
                logger.warning(f"{func.__name__} timed out after {timeout}s")
                return None
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {e}")
                return None
        return wrapper
    return decorator

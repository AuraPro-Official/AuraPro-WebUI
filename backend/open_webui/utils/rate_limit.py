import hashlib
import threading
import time
from typing import Dict, Optional

from open_webui.env import REDIS_KEY_PREFIX


class RateLimiter:
    """
    General-purpose rate limiter using Redis with a rolling window strategy.
    Falls back to in-memory storage if Redis is not available.
    """

    # In-memory fallback storage
    _memory_store: Dict[str, Dict[int, int]] = {}
    _memory_lock = threading.RLock()
    _max_memory_keys = 50000

    def __init__(
        self,
        redis_client,
        limit: int,
        window: int,
        bucket_size: int = 60,
        enabled: bool = True,
    ):
        """
        :param redis_client: Redis client instance or None
        :param limit: Max allowed events in the window
        :param window: Time window in seconds
        :param bucket_size: Bucket resolution
        :param enabled: Turn on/off rate limiting globally
        """
        self.r = redis_client
        self.limit = limit
        self.window = window
        self.bucket_size = bucket_size
        self.num_buckets = window // bucket_size
        self.enabled = enabled
        self.namespace = f'{limit}:{window}:{bucket_size}'
        self._memory_operation_count = 0

    def _key_digest(self, key: str) -> str:
        normalized = key.strip().casefold().encode('utf-8', errors='ignore')
        return hashlib.sha256(normalized).hexdigest()

    def _bucket_key(self, key: str, bucket_index: int) -> str:
        return f'{REDIS_KEY_PREFIX}:ratelimit:{self.namespace}:{self._key_digest(key)}:{bucket_index}'

    def _memory_key(self, key: str) -> str:
        return f'{self.namespace}:{self._key_digest(key)}'

    def _current_bucket(self) -> int:
        return int(time.time()) // self.bucket_size

    def _redis_available(self) -> bool:
        return self.r is not None

    def is_limited(self, key: str) -> bool:
        """
        Main rate-limit check.
        Gracefully handles missing or failing Redis.
        """
        if not self.enabled:
            return False

        if self._redis_available():
            try:
                return self._is_limited_redis(key)
            except Exception:
                return self._is_limited_memory(key)
        else:
            return self._is_limited_memory(key)

    def get_count(self, key: str) -> int:
        if not self.enabled:
            return 0

        if self._redis_available():
            try:
                return self._get_count_redis(key)
            except Exception:
                return self._get_count_memory(key)
        else:
            return self._get_count_memory(key)

    def remaining(self, key: str) -> int:
        used = self.get_count(key)
        return max(0, self.limit - used)

    def _is_limited_redis(self, key: str) -> bool:
        now_bucket = self._current_bucket()
        bucket_key = self._bucket_key(key, now_bucket)

        attempts = self.r.incr(bucket_key)
        if attempts == 1:
            self.r.expire(bucket_key, self.window + self.bucket_size)

        # Collect buckets
        buckets = [self._bucket_key(key, now_bucket - i) for i in range(self.num_buckets + 1)]

        counts = self.r.mget(buckets)
        total = sum(int(c) for c in counts if c)

        return total > self.limit

    def _get_count_redis(self, key: str) -> int:
        now_bucket = self._current_bucket()
        buckets = [self._bucket_key(key, now_bucket - i) for i in range(self.num_buckets + 1)]
        counts = self.r.mget(buckets)
        return sum(int(c) for c in counts if c)

    def _is_limited_memory(self, key: str) -> bool:
        now_bucket = self._current_bucket()
        memory_key = self._memory_key(key)

        with self._memory_lock:
            self._memory_operation_count += 1
            if self._memory_operation_count % 256 == 0:
                self._cleanup_memory(now_bucket)
            if memory_key not in self._memory_store:
                if len(self._memory_store) >= self._max_memory_keys:
                    self._cleanup_memory(now_bucket)
                    if len(self._memory_store) >= self._max_memory_keys:
                        return True
                self._memory_store[memory_key] = {}

            store = self._memory_store[memory_key]
            store[now_bucket] = store.get(now_bucket, 0) + 1

            total = sum(store.values())
            return total > self.limit

    def _cleanup_memory(self, now_bucket: int) -> None:
        min_bucket = now_bucket - self.num_buckets
        empty_keys = []
        for memory_key, store in self._memory_store.items():
            for bucket in [bucket for bucket in store if bucket < min_bucket]:
                del store[bucket]
            if not store:
                empty_keys.append(memory_key)
        for memory_key in empty_keys:
            self._memory_store.pop(memory_key, None)

    def _get_count_memory(self, key: str) -> int:
        now_bucket = self._current_bucket()
        memory_key = self._memory_key(key)
        with self._memory_lock:
            store = self._memory_store.get(memory_key)
            if store:
                min_bucket = now_bucket - self.num_buckets
                for bucket in [bucket for bucket in store if bucket < min_bucket]:
                    del store[bucket]
                if not store:
                    self._memory_store.pop(memory_key, None)
                    return 0
            return sum(store.values()) if store else 0

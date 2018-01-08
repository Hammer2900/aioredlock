import asyncio
import time
from distutils.version import StrictVersion
from aioredlock.errors import LockError

import aioredis
import re


class Instance:

    # KEYS[1] - lock resource key
    # ARGS[1] - lock uniquie identifier
    # ARGS[2] - expiration time in milliseconds
    SET_LOCK_SCRIPT = """
    local identifier = redis.call('get', KEYS[1])
    if not identifier or identifier == ARGV[1] then
        return redis.call("set", KEYS[1], ARGV[1], 'PX', ARGV[2])
    else
        return redis.error_reply('ERROR')
    end"""

    # KEYS[1] - lock resource key
    # ARGS[1] - lock uniquie identifier
    UNSET_LOCK_SCRIPT = """
    local identifier = redis.call('get', KEYS[1])
    if not identifier then
        return redis.status_reply('OK')
    elseif identifier == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return redis.error_reply('ERROR')
    end"""

    def __init__(self, host='localhost', port=6379, db=0, password=None):
        self.host = host
        self.port = port
        self.db = db
        self.password = password

        self._pool = None
        self._lock = asyncio.Lock()

        self.set_lock_script_sha1 = None
        self.unset_lock_script_sha1 = None

    @staticmethod
    async def _create_redis_pool(*args, **kwargs):
        """
        Adapter to support both aioredis-0.3.0 and aioredis-1.0.0
        For aioredis-1.0.0 and later calls:
            aioredis.create_redis_pool(*args, **kwargs)
        For aioredis-0.3.0 calls:
            aioredis.create_pool(*args, **kwargs)
        """

        if StrictVersion(aioredis.__version__) >= StrictVersion('1.0.0'):
            return await aioredis.create_redis_pool(*args, **kwargs)
        else:
            return await aioredis.create_pool(*args, **kwargs)

    async def _register_scripts(self, redis):
        futs = []
        for script in [
                self.SET_LOCK_SCRIPT,
                self.UNSET_LOCK_SCRIPT,
        ]:
            script = re.sub(r'^\s+', '', script, flags=re.M).strip()
            fut = redis.script_load(script)
            futs.append(fut)
        (
            self.set_lock_script_sha1,
            self.unset_lock_script_sha1
        ) = (r.decode() for r in await asyncio.gather(*futs))

    async def connect(self):
        """
        Get an connection for the self instance
        """
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    self._pool = await self._create_redis_pool(
                        (self.host, self.port),
                        db=self.db, password=self.password,
                        minsize=1, maxsize=100)
                    with await self._pool as redis:
                        await self._register_scripts(redis)

        return await self._pool

    async def set_lock(self, resource, lock_identifier, lock_timeout):
        """
        Lock this instance and set lock expiration time to lock_timeout
        :param resource: redis key to set
        :param lock_identifier: uniquie id of lock
        :param lock_timeout: timeout for lock in milliseconds
        :raises: LockError if lock is not acquired
        """
        try:
            with await self.connect() as redis:
                await redis.evalsha(
                    self.set_lock_script_sha1,
                    keys=[resource],
                    args=[lock_identifier, lock_timeout]
                )
        except aioredis.errors.RedisError as exc:
            raise LockError('Can not acquire lock') from exc

    async def unset_lock(self, resource, lock_identifier):
        """
        Unlock this instance
        :param resource: redis key to set
        :param lock_identifier: uniquie id of lock
        :raises: LockError if lock resource acquired with
            different lock_identifier
        """
        try:
            with await self.connect() as redis:
                await redis.evalsha(
                    self.unset_lock_script_sha1,
                    keys=[resource],
                    args=[lock_identifier]
                )
        except aioredis.errors.RedisError as exc:
            raise LockError('Can not acquire lock') from exc


class Redis:

    def __init__(self, redis_connections, lock_timeout):

        self.instances = []
        for connection in redis_connections:
            self.instances.append(
                Instance(**connection))

        self.lock_timeout = lock_timeout

    async def set_lock(self, resource, lock_identifier):
        """
        Tries to set the lock to all the redis instances

        :param resource: The resource string name to lock
        :param lock_identifier: The id of the lock. A unique string
        :return int: The elapsed time that took to lock the instances
            in milliseconds
        :raises: LockError if the lock has not been set to at least (N/2 + 1)
            instances
        """
        start_time = int(time.time() * 1000)
        lock_timeout = self.lock_timeout

        successes = await asyncio.gather(*[
            i.set_lock(resource, lock_identifier, lock_timeout) for
            i in self.instances
        ], return_exceptions=True)
        successful_sets = sum(s is None for s in successes)

        elapsed_time = int(time.time() * 1000) - start_time
        locked = True if successful_sets >= int(
            len(self.instances) / 2) + 1 else False

        if not locked:
            raise LockError('Can not acquire lock')

        return elapsed_time

    async def unset_lock(self, resource, lock_identifier):
        """
        Tries to unset the lock to all the redis instances

        :param resource: The resource string name to lock
        :param lock_identifier: The id of the lock. A unique string
        :return int: The elapsed time that took to lock the instances
            in milliseconds
        :raises: LockError if the lock has not mathing identifier in more then
            (N/2 - 1) instances
        """
        start_time = int(time.time() * 1000)

        successes = await asyncio.gather(*[
            i.unset_lock(resource, lock_identifier) for
            i in self.instances
        ], return_exceptions=True)
        successful_remvoes = sum(s is None for s in successes)

        elapsed_time = int(time.time() * 1000) - start_time
        unlocked = True if successful_remvoes >= int(
            len(self.instances) / 2) + 1 else False

        if not unlocked:
            raise LockError('Can not release lock')

        return elapsed_time

    async def clear_connections(self):
        for i in self.instances:
            i._pool.close()
        await asyncio.gather(*[
            i._pool.wait_closed() for i in self.instances
        ])

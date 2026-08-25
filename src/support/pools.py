import itertools
from concurrent.futures import ThreadPoolExecutor

import asyncio

class Pools:
    def __init__(
            self,
            qase_pool: ThreadPoolExecutor,
            source_pool: ThreadPoolExecutor,
    ):
        self.qase_pool = qase_pool
        self.source_pool = source_pool

    @staticmethod
    async def async_gen(pool: ThreadPoolExecutor, fn, *args, **kwargs):
        def gen_next(gen):
            try:
                return next(gen)
            except StopIteration:
                pass

        gen = await asyncio.wrap_future(pool.submit(fn, *args, **kwargs))
        while True:
            if (i := await asyncio.wrap_future(pool.submit(gen_next, gen))) is None:
                break
            yield i

    @staticmethod
    async def async_gen_all(pool: ThreadPoolExecutor, fn, *args, **kwargs):
        return list(itertools.chain.from_iterable(
            [_ async for _ in Pools.async_gen(pool, fn, *args, **kwargs)]
        ))

    def source(self, fn, *args, **kwargs):
        return asyncio.wrap_future(self.source_pool.submit(fn, *args, **kwargs))

    def qs(self, fn, *args, **kwargs):
        return asyncio.wrap_future(self.qase_pool.submit(fn, *args, **kwargs))

    async def source_task(self, fn, *args, **kwargs):
        return await asyncio.wrap_future(self.source_pool.submit(fn, *args, **kwargs))

    async def qs_task(self, fn, *args, **kwargs):
        return await asyncio.wrap_future(self.qase_pool.submit(fn, *args, **kwargs))

    def source_gen(self, fn, *args, **kwargs):
        return self.async_gen(self.source_pool, fn, *args, **kwargs)

    def qs_gen(self, fn, *args, **kwargs):
        return self.async_gen(self.qase_pool, fn, *args, **kwargs)

    async def source_gen_all(self, fn, *args, **kwargs):
        return await self.async_gen_all(self.source_pool, fn, *args, **kwargs)

    async def qs_gen_all(self, fn, *args, **kwargs):
        return await self.async_gen_all(self.qase_pool, fn, *args, **kwargs)

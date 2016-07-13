import asyncio
import asyncpg
import gc

from asyncpg import _testbase as tb


class TestPrepare(tb.ConnectedTestCase):

    async def test_prepare_1(self):
        st = await self.con.prepare('SELECT 1 = $1 AS test')
        self.assertEqual(st.get_query(), 'SELECT 1 = $1 AS test')

        rec = await st.fetch_row(1)
        self.assertTrue(rec['test'])
        self.assertEqual(len(rec), 1)

        self.assertEqual(False, await st.fetch_value(10))

    async def test_prepare_2(self):
        with self.assertRaisesRegex(Exception, 'column "a" does not exist'):
            await self.con.prepare('SELECT a')

    async def test_prepare_3(self):
        cases = [
            ('text', ("'NULL'", 'NULL'), [
                'aaa',
                None
            ]),

            ('decimal', ('0', 0), [
                123,
                123.5,
                None
            ])
        ]

        for type, (none_name, none_val), vals in cases:
            st = await self.con.prepare('''
                    SELECT CASE WHEN $1::{type} IS NULL THEN {default}
                    ELSE $1::{type} END'''.format(
                type=type, default=none_name))

            for val in vals:
                with self.subTest(type=type, value=val):
                    res = await st.fetch_value(val)
                    if val is None:
                        self.assertEqual(res, none_val)
                    else:
                        self.assertEqual(res, val)

    async def test_prepare_4(self):
        s = await self.con.prepare('SELECT $1::smallint')
        self.assertEqual(await s.fetch_value(10), 10)

        s = await self.con.prepare('SELECT $1::smallint * 2')
        self.assertEqual(await s.fetch_value(10), 20)

    async def test_prepare_5_unknownoid(self):
        s = await self.con.prepare("SELECT 'test'")
        self.assertEqual(await s.fetch_value(), 'test')

    async def test_prepare_6_interrupted_close(self):
        stmt = await self.con.prepare('''SELECT pg_sleep(10)''')
        fut = self.loop.create_task(stmt.fetch())

        await asyncio.sleep(0.2, loop=self.loop)

        self.assertFalse(self.con.is_closed())
        await self.con.close()
        self.assertTrue(self.con.is_closed())

        with self.assertRaisesRegex(asyncpg.ConnectionDoesNotExistError,
                                    'closed in the middle'):
            await fut

        # Test that it's OK to call close again
        await self.con.close()

    async def test_prepare_7_interrupted_terminate(self):
        stmt = await self.con.prepare('''SELECT pg_sleep(10)''')
        fut = self.loop.create_task(stmt.fetch_value())

        await asyncio.sleep(0.2, loop=self.loop)

        self.assertFalse(self.con.is_closed())
        self.con.terminate()
        self.assertTrue(self.con.is_closed())

        with self.assertRaisesRegex(asyncpg.ConnectionDoesNotExistError,
                                    'closed in the middle'):
            await fut

        # Test that it's OK to call terminate again
        self.con.terminate()

    async def test_prepare_8_big_result(self):
        stmt = await self.con.prepare('select generate_series(0,10000)')
        result = await stmt.fetch()

        self.assertEqual(len(result), 10001)
        self.assertEqual(
            [r[0] for r in result],
            list(range(10001)))

    async def test_prepare_9_raise_error(self):
        # Stress test ReadBuffer.read_cstr()
        msg = '0' * 1024 * 100
        query = """
        DO language plpgsql $$
        BEGIN
        RAISE EXCEPTION '{}';
        END
        $$;""".format(msg)

        stmt = await self.con.prepare(query)
        with self.assertRaisesRegex(asyncpg.RaiseError, msg):
            await stmt.fetch_value()

    async def test_prepare_10_stmt_lru(self):
        query = 'select {}'
        cache_max = self.con._stmt_cache_max_size
        iter_max = cache_max * 2 + 11

        # First, we have no cached statements.
        self.assertEqual(len(self.con._stmt_cache), 0)

        stmts = []
        for i in range(iter_max):
            s = await self.con.prepare(query.format(i))
            self.assertEqual(await s.fetch_value(), i)
            stmts.append(s)

        # At this point our cache should be full.
        self.assertEqual(len(self.con._stmt_cache), cache_max)
        self.assertTrue(
            all(not s.closed for s in self.con._stmt_cache.values()))

        # Since there are references to the statements (`stmts` list),
        # no statements are scheduled to be closed.
        self.assertEqual(len(self.con._stmts_to_close), 0)

        # Removing refs to statements and preparing a new statement
        # will cause connection to cleanup any stale statements.
        stmts.clear()
        gc.collect()

        # Now we have a bunch of statements that have no refs to them
        # scheduled to be closed.
        self.assertEqual(len(self.con._stmts_to_close), iter_max - cache_max)
        self.assertTrue(all(s.closed for s in self.con._stmts_to_close))
        self.assertTrue(
            all(not s.closed for s in self.con._stmt_cache.values()))

        zero = await self.con.prepare(query.format(0))
        # Hence, all stale statements should be closed now.
        self.assertEqual(len(self.con._stmts_to_close), 0)

        # The number of cached statements will stay the same though.
        self.assertEqual(len(self.con._stmt_cache), cache_max)
        self.assertTrue(
            all(not s.closed for s in self.con._stmt_cache.values()))

        # After closing all statements will be closed.
        await self.con.close()
        self.assertEqual(len(self.con._stmts_to_close), 0)
        self.assertEqual(len(self.con._stmt_cache), 0)

        # An attempt to perform an operation on a closed statement
        # will trigger an error.
        with self.assertRaisesRegex(RuntimeError, 'is closed'):
            await zero.fetch_value()

    async def test_prepare_11_stmt_gc(self):
        # Test that prepared statements should stay in the cache after
        # they are GCed.

        # First, we have no cached statements.
        self.assertEqual(len(self.con._stmt_cache), 0)
        self.assertEqual(len(self.con._stmts_to_close), 0)

        # The prepared statement that we'll create will be GCed
        # right await.  However, its state should be still in
        # in the statements LRU cache.
        await self.con.prepare('select 1')
        gc.collect()

        self.assertEqual(len(self.con._stmt_cache), 1)
        self.assertEqual(len(self.con._stmts_to_close), 0)

    async def test_prepare_12_stmt_gc(self):
        # Test that prepared statements are closed when there is no space
        # for them in the LRU cache and there are no references to them.

        # First, we have no cached statements.
        self.assertEqual(len(self.con._stmt_cache), 0)
        self.assertEqual(len(self.con._stmts_to_close), 0)

        cache_max = self.con._stmt_cache_max_size

        stmt = await self.con.prepare('select 100000000')
        self.assertEqual(len(self.con._stmt_cache), 1)
        self.assertEqual(len(self.con._stmts_to_close), 0)

        for i in range(cache_max):
            await self.con.prepare('select {}'.format(i))

        self.assertEqual(len(self.con._stmt_cache), cache_max)
        self.assertEqual(len(self.con._stmts_to_close), 0)

        del stmt
        gc.collect()

        self.assertEqual(len(self.con._stmt_cache), cache_max)
        self.assertEqual(len(self.con._stmts_to_close), 1)

    async def test_prepare_13_connect(self):
        v = await self.con.fetch_value(
            'SELECT $1::smallint AS foo', 10, column='foo')
        self.assertEqual(v, 10)

        r = await self.con.fetch_row('SELECT $1::smallint * 2 AS test', 10)
        self.assertEqual(r['test'], 20)

        rows = await self.con.fetch('SELECT generate_series(0,$1::int)', 3)
        self.assertEqual([r[0] for r in rows], [0, 1, 2, 3])

    async def test_prepare_14_explain(self):
        stmt = await self.con.prepare('SELECT typname FROM pg_type')
        plan = await stmt.explain()
        self.assertEqual(plan[0]['Plan']['Relation Name'], 'pg_type')

        stmt = await self.con.prepare(
            'SELECT typname, typlen FROM pg_type WHERE typlen > $1')
        plan = await stmt.explain(2, analyze=True)
        self.assertEqual(plan[0]['Plan']['Relation Name'], 'pg_type')
        self.assertIn('Actual Total Time', plan[0]['Plan'])

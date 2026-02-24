#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import unittest
from typing import Dict, Any
from collections import defaultdict
from time import perf_counter
from xlro.core.util.general_utils import wait_for_it, wait_for_all, StopWait

class TestWait(unittest.TestCase):
    def test_fast_stop(self):
        def fast_fail():
            raise StopWait('Fail immediately')
        start = perf_counter()
        result = wait_for_it(fast_fail)
        wait_time = perf_counter()-start
        # print(f'wait-for-it() result: {result}, time: {wait_time:.2f}')
        assert not result, 'Wait should fail!'
        assert wait_time < 1, 'Wait should fail immediately!'

    def test_wait_all_pass(self):
        values = ['a', 'b', 'c', 'd', 'e', 'f']
        orig = values
        start = perf_counter()
        timers = {v: start for v in values}
        call_counts: Dict[Any, int] = defaultdict(int)
        def sec_passed(v):
            call_counts[v] += 1
            return perf_counter()-timers[v] >= 1

        result = wait_for_all(sec_passed, values, poll=0.3, timeout=2)
        wait_time = perf_counter()-start
        # print(f'wait-for-all() result: {result}, time: {wait_time:.2f}')
        assert orig == values, 'Values should not be modified by wait-for-all()'
        assert wait_time < 2, 'Wait should pass in less that 2 seconds.'
        # print(f'call_counts: {call_counts}, values={values}')
        a_count = call_counts.pop(values[0])
        assert a_count == 5, f'First value should be checked 5 times, not {a_count}'
        assert all(count == 1 for count in call_counts.values()), f'Except first value, all should be checked only once.'

    def test_wait_all_timeout(self):
        values = ['a', 'b', 'c', 'd', 'e', 'f']
        start = perf_counter()
        call_counts: Dict[Any, int] = defaultdict(int)
        fail_index = 2
        timeout = 2
        def fail_after_n(v):
            call_counts[v] += 1
            return values.index(v) < fail_index

        result = wait_for_all(fail_after_n, values, poll=0.3, timeout=timeout)
        wait_time = perf_counter()-start
        # print(f'wait-for-all() result: {result}, time: {wait_time:.2f}')
        assert not result and wait_time > timeout, 'Test should fail after timeout of {timeout}s. (Result={result}, time={wait_time:.2f})'


if __name__ == '__main__':
    unittest.main()

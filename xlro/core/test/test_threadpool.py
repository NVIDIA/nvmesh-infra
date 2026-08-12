# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import range
import time
import random
import threading
import unittest
import logging

from xlro.core.util.thread_manager import ThreadPoolManager

tpool_logger = logging.getLogger('xlro.core.test.threadpool')


def f_no_params():
    tpool_logger.info("{0}:f_no_params start".format(threading.current_thread().name))
    time.sleep(random.uniform(0.5, 1))
    tpool_logger.info("{0}:f_no_params finished".format(threading.current_thread().name))
    time.sleep(random.uniform(0.1, 0.5))


def f_with_params(x, y):
    tpool_logger.info("{0}:f_with_params start {1} {2}".format(threading.current_thread().name, x, y))
    z = x + y
    time.sleep(random.uniform(0.5, 1))
    tpool_logger.info("{0}:f_with_params finish {1} {2} result ={3}".format(threading.current_thread().name, x, y, z))
    time.sleep(random.uniform(0.1, 0.5))
    return z


class TestEntities(unittest.TestCase):

    def test_mix_requests(self):
        with ThreadPoolManager(max_workers=3) as executor:
            res = list(executor.add_task("t1", f_no_params).add_task("t2", f_with_params, 1, 2).
                       add_task("t3", f_with_params, 3, 4).add_task("t4", f_with_params, 5, 6).add_task("t5",
                                                                                                        f_with_params,
                                                                                                        7, 8).
                       add_task("t6", f_with_params, 9, 10).start())
            tpool_logger.info(res[1].result())
            assert res[0].result() == None
            assert res[1].result() == 3

    def test_bulk_tasks(self):
        with ThreadPoolManager(max_workers=5) as executor:
            for tId in range(1, 10):
                executor.add_task("t{}".format(tId), f_no_params)
            results = executor.start()
            for res in results:
                assert res.exception() == None
                assert res.done() == True

    def test_results_by_name(self):
        with ThreadPoolManager(max_workers=2) as executor:
            res = list(executor.add_task("t1", f_no_params).add_task("t2", f_with_params, 1, 2).start())
            assert res[0].result() == None
            assert executor.get_result("t2") == 3

    def test_bulk_tasks_no_limit(self):
        with ThreadPoolManager() as executor:
            for tId in range(1, 10):
                executor.add_task("t{}".format(tId), f_no_params)
            results = executor.start()
            for res in results:
                assert res.exception() == None
                assert res.done() == True


if __name__ == '__main__':
    unittest.main()

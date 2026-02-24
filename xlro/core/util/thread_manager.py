# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait


class ThreadPoolManager(ThreadPoolExecutor):

    def __init__(self, max_workers=None, thread_name_prefix=''):
        super(ThreadPoolManager, self).__init__(max_workers, thread_name_prefix)
        self.init_pool()

    def init_pool(self):
        """
            initiate pool dicts
        :return:
        """
        self.tasks = OrderedDict()  # func:list
        self.results = OrderedDict()
        self.isShutdown = False

    def add_task(self, task_name, task, *args, **kwargs):
        """
            add task to the pool
        :param task_name: name of task
        :type task_name: str
        :param task: fucntion to be invoked by pool
        :type task: function
        :param args: list of arguments
        :type args: list
        :param kwargs: dict of arguments
        :type kwargs: dict
        :return:self
        """

        # add task
        if self.isShutdown:
            raise Exception("shutdown activate. please init pool")

        self.tasks[task_name] = (task, args, kwargs)
        self.results[task_name] = None
        return self

    def start(self, background=False):
        """
            start invoke tasks that add to pool
        :param background: run tasks without blocking.
        :type background: bool
        :return: list of respone objects
        """
        if self.isShutdown:
            raise Exception("shutdown activate. please init pool")

        for task in self.tasks.copy():
            self.results[task] = super(ThreadPoolManager, self).submit(self.tasks[task][0], *self.tasks[task][1],
                                                                       **self.tasks[task][2])
            del self.tasks[task]

        if background:
            wait(list(self.results.values()))

        # return all results in the bucket
        return list(self.results.values())

    def get_result(self, task_name):
        """
            get result by given task name
        :param task_name: task label
        :type str
        :return: object
        """
        if task_name in self.results and self.results[task_name]:
            return self.results[task_name].result()
        return None

    def shutdown(self, wait=True):
        """
            shutdown pool
        :param wait: wait until all tasks finished
        :type wait: bool
        :return:None
        """

        # close pool
        self.isShutdown = True
        super(ThreadPoolManager, self).shutdown(wait)

    def any_exceptions(self):
        return [res for res in list(self.results.values()) if res.exception()]

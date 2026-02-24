# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import object
import logging
import time

from abc import ABCMeta, abstractmethod
from threading import Thread, Event, Lock
from typing import Any,Callable,Iterable,List,Optional,Union
from enum import Enum
from xlro.core.util.general_utils import wait_for_it, IDAdapter
from functools import wraps
from xlro.core.util.terminable_thread import Thread as TThread
import uuid


from xlro.core.util.thread_manager import ThreadPoolManager
from xlro.core.util.trace_utils import PagerHandler


def operation_method(func):
    def wrapper(op, *args, **kwargs):
        op.logger.debug(f'start {func.__name__} operation of {op.__class__.__name__}')

        ret = func(op, *args, **kwargs)

        op.logger.debug(f'{func.__name__} operation of {op.__class__.__name__} has been successfully completed')
        return ret

    return wrapper


class Operation(object): # Should be ABCMeta?
    LOGGER = logging.getLogger("Operation")
    # Let operation classes override default bounce delay
    BOUNCE_DELAY = 3

    class OperationState(Enum):
        OPERATION_INITIALIZED = 0
        OPERATION_DO = 1
        OPERATION_UNDO = 2
        OPERATION_ERROR = 3

    def __init__(self):
        self.state = self.OperationState.OPERATION_INITIALIZED
        self.logger = IDAdapter(self.LOGGER.getChild(self.__class__.__name__))
        self.logger.logger.addHandler(PagerHandler.get_instance())
        self.id = self.logger.extra['id']
        self.lock = Lock()
        self.undo_ran = False

    def _set_error_and_raise(self, message, exception):
        self.state = self.OperationState.OPERATION_ERROR
        self.logger.exception(message)
        raise Exception("{} {}: {}".format(self.__class__.__name__, message, repr(exception)))

    def bounce(self, delay_or_wait_func: Union[None, float, Callable] = None, *args: Any, **kwargs: Any) -> Any:
        if delay_or_wait_func is None:
            delay_or_wait_func = self.BOUNCE_DELAY
        if isinstance(delay_or_wait_func, (int, float)):
            args = (delay_or_wait_func,)
            delay_or_wait_func = time.sleep

        with self:
            return delay_or_wait_func(*args, **kwargs)

    @operation_method
    def do(self, verify=True):
        with self.lock:
            if self.state != self.OperationState.OPERATION_INITIALIZED:
                self.logger.debug("do operation skipped")
                return

            try:
                self._do()
                self.state = self.OperationState.OPERATION_DO
            except Exception as e:
                self._set_error_and_raise("do operation failed", e)

            if verify:
                self.verify_do()

    @operation_method
    def undo(self, verify=True):
        with self.lock:
            if self.undo_ran or self.state not in (self.OperationState.OPERATION_DO, self.OperationState.OPERATION_ERROR):
                self.logger.debug("undo operation skipped")
                return

            try:
                self.undo_ran = True
                self._undo()
                self.state = self.OperationState.OPERATION_UNDO
            except Exception as e:
                self._set_error_and_raise("undo operation failed", e)

            if verify:
                self.verify_undo()

    @operation_method
    def verify_do(self):
        if self.state != self.OperationState.OPERATION_DO:
            self._set_error_and_raise("verify do failed", Exception("operation state is {0}".format(self.state)))

        try:
            self._verify_do()
        except Exception as e:
            self._set_error_and_raise("verify do failed", e)

    @operation_method
    def verify_undo(self):
        if self.state != self.OperationState.OPERATION_UNDO:
            self._set_error_and_raise("verify undo failed", Exception("operation state is {0}".format(self.state)))

        try:
            self._verify_undo()
        except Exception as e:
            self._set_error_and_raise("verify undo failed", e)

    @abstractmethod
    def _do(self):
        pass

    @abstractmethod
    def _undo(self):
        pass

    @abstractmethod
    def _verify_do(self):
        pass

    @abstractmethod
    def _verify_undo(self):
        pass

    def __enter__(self):
        self.do()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.undo()


class SimpleMultiOperation(Operation):
    # TODO - a really simple implementation - need to add different do/undo possibilties
    def __init__(self, operations, is_undo_reverse=False):
        super(SimpleMultiOperation, self).__init__()
        self.undo_reverse = lambda x: reversed(x) if is_undo_reverse else x
        self.operations = operations
        self.map: Callable[[Callable, Iterable], Iterable] = map
        self.logger.debug('Operations are {0}'.format([o.id for o in self.operations]))

    def _do(self):
        ret = self.map(lambda op: op.do(verify=False), self.operations)
        ex_msgs = [str(r) for r in ret if isinstance(ret, Exception)]
        assert not ex_msgs, 'Exceptions found after multi do operation: {}'.format(ex_msgs)

    def _verify_do(self):
        ret = self.map(lambda op: op.verify_do(), self.operations)
        ex_msgs = [str(r) for r in ret if isinstance(ret, Exception)]
        assert not ex_msgs, 'Exceptions found after multi verify do operation: {}'.format(ex_msgs)

    def _undo(self):
        ret = self.map(lambda op: op.undo(verify=False), self.undo_reverse(self.operations))
        ex_msgs = [str(r) for r in ret if isinstance(ret, Exception)]
        assert not ex_msgs, 'Exceptions found after multi undo operation: {}'.format(ex_msgs)

    def _verify_undo(self):
        ret = self.map(lambda op: op.verify_undo(), self.undo_reverse(self.operations))
        ex_msgs = [str(r) for r in ret if isinstance(ret, Exception)]
        assert not ex_msgs, 'Exceptions found after multi verify undo operation: {}'.format(ex_msgs)


class ParallelMultiOperation(SimpleMultiOperation):

    def __init__(self, *args, **kwargs):
        super(ParallelMultiOperation, self).__init__(*args, **kwargs)
        self.executor = ThreadPoolManager()
        self.map = self.executor.map


class AsyncCaller(TThread):
    # TODO - add docs
    WAIT_TIME = 60
    async_calls: List['AsyncCaller'] = []
    cls_logger = logging.getLogger('AsyncCaller')

    def __init__(self, func: Callable, stop_on_error: Optional[bool] = False, caller_param: Optional[str] = None, f_args: Optional[Union[list, tuple]] = None, f_kwargs: Optional[dict] = None) -> None:
        TThread.__init__(self, name="{}_{}".format(func.__name__, str(uuid.uuid4())[:8]))
        self.daemon = True
        self.errors: List[Exception] = []
        self.event = Event()
        self.func = func
        self.func_res = None
        self.stop_on_error = stop_on_error
        self.f_args = f_args or ()
        f_kwargs = f_kwargs or {}
        if caller_param:
            f_kwargs[caller_param] = self
        self.f_kwargs = f_kwargs
        #add self to async_calls list
        AsyncCaller.async_calls.append(self)

    def run(self):
        try:
            if not self.stopped:
                self.func_res = self.func(*self.f_args, **self.f_kwargs)
            else:
                self.logger.debug("Skip Thread {0}...".format(self.getName()))
        except Exception as e:
            self.logger.exception('got exception')
            self.errors.append(e)
            if self.stop_on_error:
                self.stop()

    def stop(self):
        self.event.set()

    @property
    def stopped(self):
        return self.event.is_set()

    @staticmethod
    def init_async_calls():
        AsyncCaller.async_calls = []

    @staticmethod
    def kill_all():
        if AsyncCaller.async_calls:
            AsyncCaller.cls_logger.debug("Set stop event on Async calls...")
            [bg_thread.stop() for bg_thread in AsyncCaller.async_calls]

            AsyncCaller.cls_logger.debug("Killing background Async calls...")
            for bg_thread in AsyncCaller.async_calls:
                if bg_thread.is_alive():
                    try:
                        bg_thread.terminate()
                        AsyncCaller.cls_logger.debug("Kill Async call {0}...".format(bg_thread.getName()))
                    except Exception as e:
                        AsyncCaller.cls_logger.exception("Kill Async call {} failed, exception:{}".format(bg_thread.getName(), e), e)

            if not wait_for_it(testfunc=lambda: not any([bg_thread.is_alive() for bg_thread in AsyncCaller.async_calls]),
                        timeout=AsyncCaller.WAIT_TIME):
                raise Exception("Async calls are alive,Failed to kill them.")

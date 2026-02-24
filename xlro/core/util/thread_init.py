# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

''' Overrides threading.Thread.__init__ to allow hooks on starting of any new thread
'''
import threading
import copy
import logging
from typing import Any,Callable,List,Optional,Tuple

# TODO: Ironically, this should be more thread-supporting :-)
_thread_funcs: List[Tuple[Callable, Any, Any, bool]] = []

# Override __init__ of Thread to allow calling thread starter functions
orig_init = threading.Thread.__init__
def _thread_init(self, *args, **kwargs):
    orig_init(self, *args, **kwargs)
    # logging.info('Init Thread: %s', self)
    t_kwargs = kwargs.copy()
    t_kwargs['thread'] = self
    for func, f_args, f_kwargs, has_thread_kwarg in _thread_funcs:
        if has_thread_kwarg:
            f_kwargs = f_kwargs.copy()
            f_kwargs['thread'] = self
        func(*f_args, **f_kwargs)
threading.Thread.__init__ = _thread_init        # type: ignore ## Extending Thread probably safer, but requires a lot of code refactoring

# logging.debug('Thread overridden. Was: %s, Is: %s', orig_init, _thread_init)

def on_start(func, *args, **kwargs):
    ''' Register a func to run then thread starts '''
    has_thread_kwarg = 'thread' in kwargs
    if has_thread_kwarg:
        kwargs.pop('thread')
    _thread_funcs.append((func, args, kwargs, has_thread_kwarg))

COPIES = '_copies_'

def _set_local_value(name, value, thread=None):
    (thread or threading.current_thread()).__dict__.setdefault(COPIES, dict())[name] = value

def _get_local_value(name: str, thread: Optional[threading.Thread] = None) -> Any:
    try:
        # return (thread or threading.current_thread())._copies_[name]
        curr_thread = thread or threading.current_thread()
        copies = getattr(curr_thread, COPIES)
        assert copies and isinstance(copies, dict)
        return copies[name]
    except:
        return None

def _copy_to_local(name: str, thread: Optional[threading.Thread] = None, **kwargs: Any) -> Any:
    value = copy.deepcopy(_get_local_value(name))
    # logging.debug('copy %s to %s.%s - %d->%d', value, thread, name, id(_get_local_value(name)), id(value))
    _set_local_value(name, value, thread)

class ThreadObj(threading.local):
    ''' A named object that will always be local to a thread, and have an 'instance' property, which is
    initialized to a copy of the value of the parent thread at the time the child is created. '''
    def __init__(self, name, init_obj):
        self.name = name
        self.instance = _get_local_value(name)
        if not self.instance:
            self.instance = init_obj
            on_start(_copy_to_local, name, thread=None)

    @property
    def instance(self):
        return self.__instance
    
    @instance.setter
    def instance(self, value):
        self.__instance = value
        _set_local_value(self.name, value)


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='(%(threadName)-0s) %(message)s',)
    # TODO: Move this to a unittest
    # This checks changes to a mutable object
    thread_path = ThreadObj('path', ['main'])
    # This checks changes to a immutable object
    thread_str = ThreadObj('str', '0')

    def show_thread_value():
        logging.debug('thread_path: %s', thread_path.instance)
        logging.debug('thread_str: %s', thread_str.instance)
        thread_path.instance.append(threading.current_thread().getName())
        thread_str.instance += '.{}'.format(id(threading.current_thread()))

    def show_and_spawn(count=0):
        show_thread_value()
        if count:
            threading.Thread(target=show_and_spawn, args=(count-1,)).start()

    threading.Thread(target=show_and_spawn, args=(3,)).start()
    threading.Thread(target=show_and_spawn, args=(2,)).start()

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import object
from typing import Any, Optional, Union
from threading import Lock
from xlro.core.util.dict_util import NOWARN_KEY


class _obj(object):
    """ recursively convert dict to object with named params """
    def __init__(self, d):
        for k, v in list(d.items()):
            if isinstance(v, (list, tuple)):
                setattr(self, k, [_obj(x) if isinstance(x, dict) else x for x in v])
            elif k != NOWARN_KEY:
                setattr(self, k, _obj(v) if isinstance(v, dict) else v)

    def __getattr__(self, item):
        # JW: let caller handle defaulting via None vs. calling getattr('property')
        # Whole purpose of this class is to allow someone to use cluster.management vs. cluster['management'] or getattr('management')
        # Step 2 is to remove the 'derived_from' altogether
        return None

def obj_to_dict(obj):
    ''' Convert above _obj type back to a dict '''
    try:
        return {k: obj_to_dict(v) for k,v in obj.__dict__.items()}
    except:
        if isinstance(obj, (list, tuple)):
            return [obj_to_dict(o) for o in obj]
        return obj

class ConfigStorage(object):
    init_lock = Lock()
    _initializer = None

    def __init__(self):
        self._obj = None

    @classmethod
    def register_initializer(cls, initializer):
        """Register a callback to lazily initialize config on first access.
        Called by xlro.infra.plugins.config_storage shim to preserve the
        auto-init behavior for infra/qa consumers."""
        cls._initializer = initializer

    def set_obj(self, obj):
        self._obj = obj

    def __getattr__(self, item):
        if self._obj is None:
            with self.init_lock:
                if self._obj is None:
                    if self._initializer:
                        self._initializer()
                    else:
                        raise RuntimeError(
                            "config_storage accessed before initialization. "
                            "Call config_storage.set_obj() or ensure ConfigPlugin is loaded.")
        return getattr(self._obj, item)


config_storage = ConfigStorage()


# config_objects_tools


def get_config(config_path: str, rconfig: Optional[Union[ConfigStorage, _obj]] = config_storage) -> Any:
    root, _, config_path = config_path.partition('.')
    rconfig = getattr(rconfig, root)
    if not config_path:
        if rconfig is None:
            raise AttributeError(root)
        return rconfig

    return get_config(config_path, rconfig)

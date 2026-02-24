# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Creating frozen data types by override only interfaces that change the objects
"""


def frozen(*args, **kwargs):
    raise Exception("Immutable object")


class FrozenList(list):
    __delitem__ = frozen
    __setitem__ = frozen
    __setslice__ = frozen
    __delslice__ = frozen
    __iadd__ = frozen
    __imul__ = frozen
    append = frozen
    insert = frozen
    pop = frozen
    remove = frozen
    reverse = frozen
    sort = frozen
    extend = frozen
    clear = frozen


class FrozenDict(dict):
    __setitem__ = frozen
    __delitem__ = frozen
    clear = frozen
    pop = frozen
    popitem = frozen
    update = frozen
    setdefault = frozen

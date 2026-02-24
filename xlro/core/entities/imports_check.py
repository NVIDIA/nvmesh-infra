#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from typing import List, Dict
from collections import defaultdict
import xlro.core.entities

def check_entities():
    import glob
    from os import path

    for mod in glob.iglob(path.join(path.dirname(__file__), '[!_]*.py')):
        __import__('xlro.core.entities.' + path.basename(mod)[:-3], globals(), locals(), ['*'], 0)

    imported_names = set(dir(xlro.core.entities))
    registry = __import__('xlro.core.entities.base', globals(), locals(), ['BaseEntity'], 0).BaseEntity.ENTITY_REGISTRY
    registered_names = set(registry.keys())
    missing: Dict[str, List] = defaultdict(list)
    for name in (registered_names-imported_names):
        missing[registry[name].__module__.rpartition('.')[2]].append(name)
    return missing

if __name__ == '__main__':
    for module, imports in check_entities().items():
        print('from', module, 'import', ', '.join(imports))

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import object
import json


class PageRef(object):
    def __init__(self, pslice, role):
        from xlro.tools.nvmesh_edit.util import role2rolename
        self.pslice = pslice
        self.role = role
        self.blocks = pslice.ordered_content[role]
        self.vlba = self.pslice.vlbs.addr + (self.role * pslice.praid.volume.snake) \
            if self.role < self.pslice.praid.dataDisks else self.pslice.vlbs.addr
        self.rolename = role2rolename(role, pslice.praid)

    def __str__(self):
        return str(self.pslice) + ' + ' + str(self.role)


class RMBInfo(object):
    def __init__(self, blockset, ordered_locks=None):
        ordered_locks = ordered_locks or blockset.get_rmbinfo()
        d = blockset.praid.dataDisks
        for role, lock in enumerate(ordered_locks):
            if role == 0 or role >= d:
                role_name = 'D' + str(role) if role < d else 'P' + str(role - d)
                setattr(self, role_name, lock)

    def __getitem__(self, item):
        return self.__dict__.get(item, None)

    def __str__(self):
        return json.dumps({r: str(l) for r, l in self.__dict__.items()}, indent=2)

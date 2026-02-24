# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
from os import path
from typing import Any

from xlro.core.entities.base import *
from xlro.core.entities import BaseEntity
from xlro.core.entities.sdk_base import SDKEntity, sdk_entity
from xlro.core.entities.host import Host
from xlro.core.entities.network import Node
from xlro.core.util.ssh import Connection


@entity(sourcetypes=[SourceTypes.PROC, SourceTypes.MANAGEMENT, SourceTypes.OS])
class NvNode(BaseEntity):
    name : str = PropertySpec(str, key=True)
    version : str = PropertySpec(str)

    @prop_loader(SourceTypes.OS, ['version'])
    def load_version_from_proc(self):
        return {'version': self.host.package_version(
            'nvmesh-client' if 'nvmesh-client' in self.host.list_installed_nvmesh_rpms() else 'nvmesh-core'
        )}

    # some properties to support older APIs
    @property
    def host(self):
        return Host.instance(name=self.name)

    @property
    def connection(self) -> Connection:
        return self.host.connection

    def reboot(self, force_level=0, wait=True):
        return self.host.reboot(force_level, wait)

    def ipmi(self, cmd, wait=True):
        return self.host.ipmi(cmd, wait)

    def proc_content(self, proc_prefix: str, proc_path: str, no_cache: bool = False, *args: Any, **kwargs: Any) -> str:
        return self.host.proc_content(path.join(proc_prefix, proc_path), no_cache, *args, **kwargs)

    @property
    def node(self):
        return Node.instance(name=self.name)

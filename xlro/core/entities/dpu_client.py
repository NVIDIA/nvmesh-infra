# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import Iterable, Dict, Any

from xlro.core.entities import ExternalClient, Volume
from xlro.core.entities.base import entity, SourceTypes

@entity(sourcetypes=[SourceTypes.PROC])
class DpuClient(ExternalClient):
    def do_setup(self):
        pass

    def do_teardown(self):
        pass

    def do_bind(self, volumes: Iterable[Volume], *args, **kwargs) -> Dict[Volume, Any]:
        pass

    def do_unbind(self, volumes: Iterable[Volume], *args, **kwargs) -> Iterable[Volume]:
        pass


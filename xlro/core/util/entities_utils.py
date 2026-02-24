# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import Optional

from xlro.core.entities import Target, Service


def get_toma_leader_service(target: Target) -> Optional[Service]:
    """variation on Target.get_toma_leader but returns service rather then name:str"""
    out, err, code = target.connection.execute('sudo cat /proc/nvmeibs/toma_status/leader')
    leader_target_name = out.strip()
    if code == 0 and leader_target_name and ' ' not in leader_target_name:
        toma_leader_service = Target.instance(name=leader_target_name).services['toma']
        return toma_leader_service
    return None

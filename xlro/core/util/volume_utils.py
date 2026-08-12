# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import re
import json
import random

from collections.abc import Mapping
from itertools import count
from functools import partial
from xlro.core.entities import Manager, Volume, Client, SourceTypes
from typing import Dict, Optional, Any, Union, Sequence, Generator, List
from xlro.core import infra_conf
from xlro.core.entities.etypes import Size
from xlro.core.entities.sdk_base import SdkException

vol_defs = infra_conf.root.volume_defs
VOL_PREFIX = 'v'
vol_series = random.randint(1000, 9999)
vol_counter = count(1)


def get_volume_dict(volume_spec: Union[str, Dict], **kwargs: Any) -> dict:
    vol_defs_dict = vol_defs._conf.serialize_to_dict()
    vdefs = vol_defs_dict['_defaults_']

    if isinstance(volume_spec, Mapping):
        vdefs.update(volume_spec)
    else:
        try:
            vdefs.update(vol_defs_dict[volume_spec].copy())
        except KeyError:
            match = re.match(r'^(?P<prefix>s?ec)-(?P<dataBlocks>\d+)D[-+](?P<parityBlocks>\d+)P$', volume_spec)
            if not match:
                raise
            base = 'sec' if match.group('prefix') == 'sec' else 'ec'
            vdefs.update(vol_defs_dict[base])
            vdefs.update({k: int(v) for k, v in match.groupdict().items() if k != 'prefix'})
    vdefs.update(kwargs)
    return vdefs


def volume_from_spec(volume_spec: Union[Dict, str], mgmt: Optional[Manager] = None, **kwargs: Any) -> Volume:
    """ Generate a new Volume instance (not-yet-created) from a volume-spec + any override keyword args.  """
    vdefs = get_volume_dict(volume_spec)
    vdefs.update(kwargs)
    mgmt = mgmt or Manager.get_manager()
    if 'name' not in vdefs:
        vdefs['name'] = '{}{}-{}'.format(VOL_PREFIX, vol_series, next(vol_counter))
    volume = Volume.instance(name=vdefs['name'], mgmt=mgmt)
    volume.set_properties(vdefs)
    return volume

# Utilities moved from qa packages
def get_volume_data_segments(volume):
    data_segments = []
    for chunk in volume.chunks:
        for pRaid in chunk.pRaids:
            data_segments.extend(pRaid.get_dataSegments())
    return data_segments


def get_volume_data_drives(volume):
    d_drives = []
    for d_seg in get_volume_data_segments(volume):
        if d_seg.drive not in d_drives:
            d_drives.append(d_seg.drive)
    return d_drives


def get_volumes_data_drives(volumes):
    d_drives = []
    for volume in volumes:
        for d_drive in get_volume_data_drives(volume):
            if d_drive not in d_drives:
                d_drives.append(d_drive)
    return d_drives

def get_drive_counters(client, drive, counter='iostats'):
    # type: (Client, str ,str) -> Dict
    """A general utility method for getting an attachment's counter. added to the request of Eldad"""
    counter_path = "cat {}/disks/{}/{}.json".format(client.proc, drive, counter)
    out = client.connection.err2exc(client.connection.execute(counter_path))
    return json.loads(out)


def get_drive_total_overeager(client, drive):
    # type: (Client, str) -> int
    counters = get_drive_counters(client, drive)
    return counters["overeager"]


def check_overeager_drives(mgr):
    # type: (Manager) -> Generator[Sequence, None, None]
    for client in mgr.clients:
        if client.isUmClient:
            continue
        attached_drives = client.load_attached_drives()
        client_overeagers = partial(get_drive_total_overeager, client)
        for drive in attached_drives:
            try:
                yield client.name, drive, client_overeagers(drive)
            except:
                yield "Error",drive

def any_drives_on_node(drives, node):
    for drive in drives:
        if drive.target.name == node.name:
            return True
    return False

def check_for_resize(v):
    if v.rest_info.kwargs.get('ops', {}).get('extend', None):
        # Extend required instead of update capacity
        try:
            local_capacity = v.get_property('capacity', SourceTypes.LOCAL)
            mgmt_capacity = v.get_property('capacity', SourceTypes.MANAGEMENT)
            if local_capacity and local_capacity != mgmt_capacity:
                v.logger.info(f'Using extend to modify capacity from {mgmt_capacity} to {local_capacity}')
                ret = v.do_operation(v.mgmt, 'extend', [v], capacity=Size(Volume.MAX_STR) if isinstance(v, Volume) and local_capacity == Volume.MAX_INT else local_capacity)
                if not ret[0]['success']:
                    raise SdkException(ent=v, reason=f'Extend failed. {ret[0].get("error", ret)}')
                v.reset_property('capacity', [SourceTypes.LOCAL])
        except AttributeError:
            pass

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import object
from typing import List
from xlro.core.entities.base import *
from xlro.core.entities.volume import Volume
from xlro.core.entities.drive import Drive
from xlro.core.entities.host import Host

from datetime import datetime
import uuid


@entity(sourcetypes=[SourceTypes.BINARY_TRACE])
class PraidTopology(BaseEntity):
    volume : Volume = PropertySpec(Volume, key=True)
    version : int = PropertySpec(int, key=True)
    index : int = PropertySpec(int, key=True)
    chunk_index : int = PropertySpec(int, key=True)
    segments : List['SegmentTopology'] = PropertySpec(['SegmentTopology'], default=[])


@entity(sourcetypes=[SourceTypes.BINARY_TRACE])
class SegmentTopology(BaseEntity):
    praid_topology : PraidTopology = PropertySpec(PraidTopology, key=True)
    index : int = PropertySpec(int, key=True)
    acm : str = PropertySpec(str)
    disk : Drive = PropertySpec(Drive)
    disk_state : int = PropertySpec(int)


class ClientTomaMessage(object):
    def __init__(self, msg_type, reason, volume, chunk, praid, segment, time_stamp):
        self.msg_type = msg_type
        self.reason = reason
        self.volume = Volume.instance(name=volume)
        self.chunk_index = chunk
        self.praid_index = praid
        self.segment_index = segment
        self.time_stamp = datetime.strptime(time_stamp, '%H:%M:%S.%f')

    def to_dict(self):
        return {k: str(v) for k, v in list(self.__dict__.items())}


class TRMessage(ClientTomaMessage):
    def __init__(self, msg_type, reason, volume, chunk, praid, segment, config_version, conversation_id, time_stamp):
        super(TRMessage, self).__init__(msg_type, reason, volume, chunk, praid, segment, time_stamp)
        self.config_version = config_version
        self.conversation_id = int(conversation_id, 16)


class RTMessage(ClientTomaMessage):
    def __init__(self, msg_type, reason, volume, chunk, praid, segment, segment_uuid, disk_host, praid_version,
                 time_stamp):
        super(RTMessage, self).__init__(msg_type, reason, volume, chunk, praid, segment, time_stamp)
        self.segment_uuid = uuid.UUID(segment_uuid)
        self.praid_version = praid_version
        self.disk_host = Host.instance(name=disk_host)

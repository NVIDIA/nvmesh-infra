#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import List, Type, Optional, TypeVar, Tuple, Dict
from uuid import UUID
from xlro.core.entities import Manager, SDKEntity, SourceTypes, Volume
from xlro.core.entities.base import PropertySpec
from xlro.core.entities.sdk_base import sdk_entity
from xlro.core.entities.etypes import Size
from xlro.core.sdk.Utils import MongoObj

RE = TypeVar('RE', bound='SDKEntity')

@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class Cluster(SDKEntity):
    HEALTH_STATUSES = ['healthy', 'alarm', 'critical']

    dbUUID : UUID = PropertySpec(UUID, key=True)
    id : str = PropertySpec(str, default='')
    totalSpace : Size = PropertySpec(Size)
    allocatedSpace : Size = PropertySpec(Size)
    freeSpace : Size = PropertySpec(Size)

    def __init__(self, *args, **kwargs):
        super(Cluster, self).__init__(*args, **kwargs)

    @classmethod
    def _genkey(cls, source: Optional[str] = None, kwargs: Optional[dict] = None) -> str:
        """ This is a kludge of an entity anyway... """
        from xlro.core.entities import Manager
        if kwargs:
            kwargs.setdefault('dbUUID', Manager.get_manager().connection.dbUUID)
        return super(Cluster, cls)._genkey(source, kwargs)

    @classmethod
    def _makeGet(cls, mgr: 'Manager', routes) -> Tuple[Dict, List[Dict]]:
        return ({}, [1]) if 'count' in routes else cls._makeRequest('get', mgr, ['getClusterStatus?skipLogs=true'])

    @staticmethod
    def get_health_occurences_from_data(data: dict, buckets: Optional[List[str]] = None) -> List[str]:
        keys = buckets if buckets is not None else Cluster.HEALTH_STATUSES
        return [f"{k.replace('_', ' ').title()}: {data.get(k, 0)}" for k in keys]

    @property
    def targets_summary(self):
        return Cluster.get_health_occurences_from_data(self.get_property('servers'))

    @property
    def clients_summary(self):
        return Cluster.get_health_occurences_from_data(self.get_property('clients'))

    @property
    def volumes_summary(self):
        return Cluster.get_health_occurences_from_data(self.get_property('volumes'))

    @property
    def drives_summary(self):
        return Cluster.get_health_occurences_from_data(self.get_property('drives'))

    def _fetch_volume_counters(self) -> dict:
        """ Fetch CDV/TPV health buckets via /getVolumeCounters. Cached per instance. """
        if getattr(self, '_volume_counters_cache', None) is None:
            err, out = Cluster._makeRequest('get', self.mgmt, ['getVolumeCounters'])
            if err:
                self.logger.warning(f'getVolumeCounters failed: {err}')
                self._volume_counters_cache = {}
            else:
                # _makeRequest wraps a single dict response into a list; unwrap.
                self._volume_counters_cache = out[0] if isinstance(out, list) and out else (out or {})
        return self._volume_counters_cache

    @property
    def cdvs_summary(self):
        data = self._fetch_volume_counters().get('cdvCount') or {}
        return Cluster.get_health_occurences_from_data(data, ['healthy', 'almost_full', 'alarm', 'critical'])

    @property
    def tpvs_summary(self):
        data = self._fetch_volume_counters().get('tpvCount') or {}
        return Cluster.get_health_occurences_from_data(data, ['healthy', 'alarm', 'critical', 'detached'])

    @property
    def orig_volume_segments_summary(self):
        """Original implementation for benchmarking purposes"""
        disk_segments = 0
        data_segments = 0
        proj = [MongoObj(field='name', value=1), MongoObj(field='chunks', value=1)]
        for v in Volume.sdk_get(mgmt=self.mgmt, projection_mongo_objs=proj):
            for c in v.chunks:
                for p in c.pRaids:
                    disk_segments += len(p.diskSegments)
                    data_segments += len(p.get_dataSegments())
        return f'Total: {disk_segments}, Data: {data_segments}'

    @property
    def volume_segments_summary(self):
        disk_segments = 0
        data_segments = 0
        proj = [MongoObj(field='name', value=1), MongoObj(field='chunks.pRaids.diskSegments.type', value=1)]
        vols_data = Volume._sdk_get(count=0, page=0, mgmt=self.mgmt, projection_mongo_objs=proj)
        for vol in vols_data:
            for chunk in vol.get("chunks", []):
                for praid in chunk.get("pRaids", []):
                    segs = praid.get("diskSegments", [])
                    disk_segments += len(segs)
                    data_segments += sum(1 for seg in segs if seg.get("type") == "data")

        return f'Total: {disk_segments}, Data: {data_segments}'

def main():
    from xlro.core.util.cli_util import CLIArgumentParser
    args = CLIArgumentParser(require_manager=True).parse_args()
    print(args.manager.cluster.get_property('totalSpace', no_cache=True))
    Cluster.do_operation(args.manager, 'update-cluster-id', ['foo-bar'])
    print(args.manager.cluster.get_property('totalSpace', no_cache=True))

if __name__ == '__main__':
    main()

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import Dict, Any
from collections import defaultdict
from typing import Dict, Any
from xlro.core.util.watchers import PyWatchMonitor, MultiWatcher


class CaserEntry(object):
    fmt = '{seg}-{cid}:{pver}:{dele}:{occu}:{alloc}:{load}'

    def __init__(self, segment, client_id, praid_version, is_delete, occupied, allocated, load):
        self.segment = segment
        self.client_id = client_id
        self.is_delete = is_delete == '*'
        self.praid_version = int(praid_version, 16)
        self.allocated = int(allocated)
        self.occupied = int(occupied)
        self.load = int(load)

    def __repr__(self):
        return self.fmt.format(seg=self.segment, cid=self.client_id, pver=self.praid_version, dele=self.is_delete,
                               occu=self.occupied, alloc=self.allocated, load=self.load)


def get_caser_info(res):
    caser_map = defaultdict(list)
    for seg_cid, data in res.items():
        seg, cid = seg_cid.split('-')
        if cid == '0':
            continue
        caser_map[seg].append(CaserEntry(seg, cid, *data))
    return caser_map


class CaserWatcherMonitor(PyWatchMonitor):
    proc = '/proc/nvmeibs/toma_status/caser'

    def __init__(self, host, **kwargs):
        super(CaserWatcherMonitor, self).__init__(host, sep=':',
                                                  cmd=f"awk \"/seg=/ {{seg=\$1}} \$5 ~ /praid_ver=.*/ {{split(\$5, a, \\\"=\\\"); pver=a[2]; print seg 0,pver,0,0,0}} \$4 ~ /^[0-9]+\$/ && seg ~ /seg=/ {{print seg \$1,pver,\$2,\$3,\$4}}\" {self.proc} 2>&1",
                                                  pattern=r'seg=(?P<seg>\w+):(?P<cid>\w+)(?P<dele>\**) (?P<pver>\w+) (?P<occu>\d+) (?P<alloc>\d+) (?P<load>\d+)',
                                                  format=CaserEntry.fmt,
                                                  resilient=True, **kwargs)

    @property
    def caser_info(self):
        return get_caser_info(self.results)


class CaserMultiWatcher(MultiWatcher):
    seg2praid_ver : Dict = defaultdict(int)

    def __init__(self, hosts, *args, **kwargs):
        super(CaserMultiWatcher, self).__init__(*args, **kwargs)
        list(map(self.add_monitor, [CaserWatcherMonitor(host) for host in hosts]))

    @property
    def caser_info(self):
        return get_caser_info(self.results)

    @property
    def results(self):
        # TODO: This type conflicts with MultiWatcher.  Key is assumed to be a tuple. type: () -> Dict[str, Any]
        res = {k: v for mon in self.watchers() for k, v in mon.results.items()}
        # sort descending by segment and praid version
        sorted_res = sorted(res.items(), key=lambda r: (r[0].split('-')[0], r[1][0]), reverse=True)
        result = {}
        for seg_cid, data in sorted_res:
            seg, cid = seg_cid.split('-')
            praid_ver = int(data[0], 16)
            if praid_ver < self.seg2praid_ver[seg]:
                # get rid of outdated messages
                continue

            self.seg2praid_ver[seg] = praid_ver
            if cid == '0':
                # get rid of the "dummy" client events after setting new praid version for segment
                continue

            result.update({seg_cid: data})
        return result

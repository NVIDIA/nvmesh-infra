# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl
from prometheus_client.parser import text_string_to_metric_families
from requests import Session
from collections import defaultdict
from typing import Dict


class MetricsDiag(DiagModule):
    description = "Metrics Check"

    def diagnose(self):
        count_by_status: Dict[str, int] = defaultdict(int)
        vol_segs_by_status: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        try:
            with Session() as session:
                response = session.get(f'http://{self.node.name}:9300')
                for family in text_string_to_metric_families(response.text):
                    for sample in family.samples:
                        if sample.value > 0:
                            if sample.name == 'nvmesh_client_volume_attached_total':
                                status = '{io_state} ({visibility})'.format(**sample.labels)
                                count_by_status[status] += int(sample.value)
                            elif sample.name == 'nvmesh_client_volume_segment_statuses':
                                status = 'act={act}, paused={paused}'.format(**sample.labels)
                                vol_segs_by_status[sample.labels['volume_name']][status] += int(sample.value)
        except Exception as e:
            self.add_message(f'Failed to scrape metrics. ({e.__class__.__name__.rpartition(".")[2]})', MsgLvl.ERROR)


        # Show attached volumes by status
        for status, count in count_by_status.items():
            # TODO: need to map statuses to msg-levels
            lvl = MsgLvl.SUCCESS if status.startswith('Live') and not 'no IO' in status else MsgLvl.WARNING
            self.add_message(f'Attached volumes in status {status}: {count}', msg_level=lvl)

        # Show local volume segments by status
        for vol, seg_statuses in sorted(vol_segs_by_status.items()):
            for seg_stat, count in seg_statuses.items():
                self.add_message(f'{vol}: {count} segments {seg_stat}')

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import Dict
from collections import defaultdict
from xlro.core.entities import Target
from xlro.tools.nvmesh_diag.util import ClusterDiagnostic, DiagArgs, MsgLvl

class TomaCheck(ClusterDiagnostic):
    description = "Toma Leaders"

    def validate(self):
        # Check if UM service is running - skip TOMA check for UM nodes
        if self.is_um():
            self.skip("UM service detected. Skipping TOMA check as TOMA doesn't run on UM.")
            return

        # Use SSH to check for TOMA service on nodes directly
        targets = []
        for node in DiagArgs.nodes:
            # Check if TOMA service is installed/running via SSH
            try:
                # Check if service exists and is running
                cmd = f"systemctl is-active nvmeshtoma.service || echo inactive"
                out, _, _ = self.run_cmd(cmd)
                is_toma_service = out.strip() == "active"
                if is_toma_service:
                    targets.append(Target.instance(name=node.name))
            except Exception as e:
                self.add_message(f"Failed to check TOMA service on {node.name}: {str(e)}", MsgLvl.WARNING)

        if not targets:
            self.add_message("No TOMA services found on specified nodes", MsgLvl.WARNING)
            return

        # Existing TOMA leader check logic
        leaders = defaultdict(int)
        leader = None
        for t in targets:
            try:
                leader = t.get_toma_leader()
                leaders[leader] += 1
            except Exception as e:
                self.add_message(f"Failed to get TOMA leader from {t.name}: {str(e)}", MsgLvl.WARNING)

        if leader and len(leaders) == 1:
            leader_target = Target.instance(name=leader)
            toma_heart_bit = float(leader_target.toma_config['raft_leader_heartbeat_timeout_usec']) * 3 / 1_000_000
            for peer_node, last_vote_time in leader_target.get_toma_peer_node_to_last_vote_time():
                if float(last_vote_time) > toma_heart_bit:
                    self.add_message(f'RAFT voting time is not up-to-date for TOMA {peer_node}. value: {last_vote_time}, expected less than: {toma_heart_bit}', MsgLvl.WARNING)

            self.add_message(f'All {len(targets)} targets agree on TOMA Leader: {leader} and RAFT voting is up-to-date', MsgLvl.SUCCESS)
        else:
            toma_msg = ', '.join([f'{count} targets have leader: "{leader or ""}"' for leader, count in leaders.items()])
            self.add_message(f'Missing/inconsistent TOMA Leader: {toma_msg}', MsgLvl.ERROR)

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import re
from time import sleep
from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl
from xlro.core.entities import Client
from xlro.core.util import scanner

class Attachments(DiagModule):
    description = "Attachments"
    SCAN_LOCK_FILE_NAME = "scan_locks_ec"

    def discover(self):

        # Get attached volumes and their attachment objects
        self.diag_info.attachments = {}
        self.diag_info.volumes_in_use = []
        self.get_attached_volumes()

        self.use_scan_locks()

    def validate(self):
        # Validate collected data
        issues_found = False

        # Check if attachments are loaded
        if not self.diag_info.attachments:
            self.add_message("No volumes are attached on this client", MsgLvl.WARNING)
            return

        for volume, attachment in self.diag_info.attachments.items():
            # Check for topology errors using Attachment entities
            if hasattr(attachment, 'topo') and attachment.topo: # Loading `topo` triggers load_from_proc which loads all properties from status.json
                topo = attachment.topo
                # Check for topology errors using Attachment entities
                if hasattr(topo, 'err') and topo.err != 0:
                    issues_found = True
                    self.add_message(f"Volume {volume} has topology error: {topo.err}", MsgLvl.ERROR)

                    # Check for stuck topology
                    if hasattr(topo, 'cur') and hasattr(topo, 'freed'):
                        current_topo = topo.cur
                        last_freed = topo.freed
                        if int(last_freed) + 1 != int(current_topo):
                            self.add_message(f"Volume {volume} has stuck topology: current={current_topo}, last_freed={last_freed}", MsgLvl.ERROR)

                # Check for non-active segments - using chunk from topo API
                if hasattr(topo, 'chunks') and topo.chunks:
                    non_active_segs = {}
                    for chunk in topo.chunks:
                        for pr in chunk.prs:
                            for seg in pr.segs:
                                if seg.act == 0 or seg.acm.strip() == 'D' or seg.disk.paused == 1:
                                    status_string = f"Active:{seg.act}  ACM:{seg.acm}  {'Paused' if seg.disk.paused == 1 else 'Not Paused!!'}"
                                    non_active_segs[(chunk.ci, pr.ri, seg.si)] = status_string
                                    self.add_message(f"  Chunk {chunk.ci} Stripe {pr.ri} Segment {seg.si}: {status_string}", MsgLvl.ERROR)
                    if non_active_segs:
                        issues_found = True
                        self.add_message(f"Volume {volume} has {len(non_active_segs)} non-active segments", MsgLvl.ERROR)

            # Check for IO errors
            if hasattr(attachment, 'status_counters') and attachment.status_counters:
                status_counters = attachment.status_counters
                errors = ['critical', 'detach', 'ignore', 'rider_cancel', 'illegal_trims', 'other']
                for error in errors:
                    if status_counters.get(error, 0) > 0:
                        issues_found = True
                        self.add_message(f"Volume {volume} has failed IOs: Type{error}[{status_counters[error]}]")

        # Check for stuck detach operations
        stuck_detaches = self.get_stuck_detaches()
        if len(stuck_detaches) > 0:
            issues_found = True
            for volume in stuck_detaches:
                self.add_message(f"Volume {volume} has stuck detach operation", MsgLvl.ERROR)

        # Check for volumes with no IO activity
        no_io_volumes = self.get_io_activity()
        if len(no_io_volumes) > 0:
            self.add_message(f"Volumes with no IO activity in the last check: {', '.join(no_io_volumes)}", MsgLvl.WARNING)

        if not issues_found:
            self.add_message("All volume checks passed", MsgLvl.SUCCESS)

    def details(self):
        # Additional details about volumes using Attachment entities
        for volume, attachment in self.diag_info.attachments.items():
            self.add_message(f"Volume {volume} details:")

            # Status details from Attachment
            self.add_message(f"  Status: {getattr(attachment, 'dbg', 'Unknown')}")

            # Topology details
            if hasattr(attachment, 'topo') and attachment.topo:
                topo = attachment.topo
                if hasattr(topo, 'cur') and hasattr(topo, 'freed'):
                    self.add_message(f"  Current topology index: {topo.cur}")
                    self.add_message(f"  Last freed topology index: {topo.freed}")
                if hasattr(topo, 'nr'):
                    self.add_message(f"  Number of reconfiguring segments: {topo.nr}")

            # Client processes details
            self.add_message(f"  Volume in use: {'Yes' if volume in self.diag_info.volumes_in_use else 'No'}") # Detailed list of client processes using this volume is in the logger.

    def get_attached_volumes(self):
        client = Client.instance(name=self.nodename)
        self.diag_info.attachments = client.attachments
        self.diag_info.volumes_in_use = client.volumes_in_use()
        self.add_message(f"Found {len(self.diag_info.attachments)} attached volumes, {len(self.diag_info.volumes_in_use)} volumes in use")

    def get_stuck_detaches(self):
        # Check for stuck detach operations in system logs
        out, err, code = self.run_cmd("sudo grep -E 'nvmeibc_block_busy|nvmeibc_del_blkdev' /var/log/messages")
        if code != 0:
            return []

        detaching_volumes = []
        for line in out.split('\n'):
            if not line:
                continue

            if "nvmeibc_block_busy" in line:
                volume = line.split(": ")[-2].strip()
                if volume not in detaching_volumes:
                    detaching_volumes.append(volume)
            elif "nvmeibc_del_blkdev" in line:
                volume = line.split(": ")[-1].split()[0].strip()
                if volume in detaching_volumes:
                    detaching_volumes.remove(volume)

        return detaching_volumes

    def use_scan_locks(self):
        # Run scan_locks for every disk in volume
        self.add_message("Running scan_locks for every disk in volume:")
        for _, attachment in self.diag_info.attachments.items():
            locks_summary = scanner.get_vol_locks_summary(attachment.volume)
            if locks_summary:
                self.add_message(f"  Volume {attachment.volume} has locks: {locks_summary}")

    def get_io_activity(self):
        # Use Attachment iostats_counters to check for IO activity
        no_io_volumes = []

        # Get initial IO stats from Attachment entities
        iostats_first = {}
        for volume, attachment in self.diag_info.attachments.items():
            try:
                iostats_first[volume] = attachment.get_property('iostats_counters', no_cache=True).copy()
            except Exception as e:
                self.add_message(f"Could not get initial IO stats for volume {volume}: {e}", MsgLvl.WARNING)
                iostats_first[volume] = {}

        # Wait for a moment to allow new IO activity
        sleep(2)

        # Check for differences in IO stats
        for volume, attachment in self.diag_info.attachments.items():
            try:
                # Refresh the attachment to get updated counters
                iostats_second = attachment.get_property('iostats_counters', no_cache=True).copy()

                # Compare first and second stats
                if volume in iostats_first:
                    same = True
                    for key, value in iostats_second.items():
                        if key in iostats_first[volume] and iostats_first[volume][key] != value:
                            same = False
                            break

                    if same and iostats_first[volume] == iostats_second:
                        no_io_volumes.append(volume)

            except Exception as e:
                self.add_message(f"Could not get updated IO stats for volume {volume}: {e}", MsgLvl.WARNING)
                continue

        return no_io_volumes

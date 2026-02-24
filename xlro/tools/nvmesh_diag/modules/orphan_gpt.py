# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.core.entities import Target
from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl


class OrphanGPT(DiagModule):
    description = "Orphan GPT Entries (Bad Segments)"

    def discover(self):
        """
        Send signal 10 (SIGUSR1) to TOMA process to trigger stats dump,
        then locate the most recent toma_20* file.
        """
        # Initialize with safe defaults before any run_cmd calls that might skip/raise
        self.diag_info.toma_running = False
        self.diag_info.stat_file = None

        # Check if TOMA service is running (doesn't require root)
        cmd = "systemctl is-active nvmeshtoma.service || echo inactive"
        status, _, _ = self.run_cmd(cmd, is_sudo=False)
        if status.strip() != "active":
            return

        self.diag_info.toma_running = True

        # Send signal 10 (SIGUSR1) to TOMA process to trigger stats file generation
        target = Target.instance(name=self.nodename)
        target.services['toma'].kill(signal=10)

        # Wait briefly for the stats file to be written
        self.run_cmd("sleep 1", is_sudo=False, print_err=False)

        # Find the most recent toma_20* file
        find_stat_file_cmd = "ls -tr /var/log/nvmesh/toma_20* 2>/dev/null | tail -1"
        stat_file, _, code = self.run_cmd(find_stat_file_cmd, print_err=False)
        self.diag_info.stat_file = stat_file.strip() if code == 0 and stat_file.strip() else None

    def validate(self):
        """
        Check for bad segments (orphan GPT entries) in the TOMA stats file.
        Bad segments are identified as lines containing '- seg' with 'vol=???'.
        """
        if not getattr(self.diag_info, 'toma_running', False):
            self.skip("TOMA service is not running. Skipping orphan GPT check.")
            return

        if not getattr(self.diag_info, 'stat_file', None):
            self.add_message("No TOMA stats file (toma_20*) found in /var/log/nvmesh/", MsgLvl.WARNING)
            return

        # Count bad segments: lines matching '- seg' with 'vol=???'
        count_cmd = f"grep -E '\\- seg' {self.diag_info.stat_file} | grep 'vol=???' | wc -l"
        count_out, _, code = self.run_cmd(count_cmd, print_err=False)

        if code != 0:
            self.add_message(f"Failed to analyze stats file: {self.diag_info.stat_file}", MsgLvl.ERROR)
            return

        try:
            n_bad_segs = int(count_out.strip())
        except ValueError:
            self.add_message(f"Failed to parse bad segment count from output: {count_out}", MsgLvl.ERROR)
            return

        if n_bad_segs == 0:
            self.add_message(f"No orphan GPT entries found (stats file: {self.diag_info.stat_file})", MsgLvl.SUCCESS)
        else:
            self.add_message(f"Has {n_bad_segs} corrupted entries (orphan GPT) in {self.diag_info.stat_file}", MsgLvl.ERROR)


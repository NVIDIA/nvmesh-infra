# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl, DiagArgs


class Selinux(DiagModule):
    description = "SELinux"

    def validate(self):
        if self.get_info('platform').platform == 'ubuntu':
            self.skip('SELinux unsupported on ubuntu')

        out, err, code = self.run_cmd("sestatus || getenforce")
        if code != 0:
            self.skip("Couldn't find SElinux. Skipping this step.")

        if "disabled" in out.lower():
            self.add_message("SELinux Disabled", MsgLvl.SUCCESS)
        else:
            self.add_message("SELinux active. It's recommended to disable SELinux!", MsgLvl.WARNING)
            if DiagArgs.verbose:
                for line in out.splitlines():
                    self.add_message(line)
            self.suggest_fix("Do you want to disable SELinux now?", True, self.run_cmd, self.node,
                             "sed -i 's/^SELINUX=enforcing/SELINUX=disabled/' /etc/selinux/config")

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl


class PlatformCheck(DiagModule):
    description = "Platform Check"

    def discover(self):
        self.diag_info.platform = None
        self.diag_info.pkg_manager = None
        if not self.get_info('host').has_shell:
            self.skip(f'no shell access on {self.node}')

        try:
            self.diag_info.platform = self.get_os_platform()
            platform = self.diag_info.platform
            if self.diag_info.platform:
                self.add_message(f'{self.node.name} '
                                 f'platform: {platform}, '
                                 f'OS: {self.node.os_info["PRETTY_NAME"]}, '
                                 f'Kernel: {self.node.info["kernel-release"]}')
                self.diag_info.pkg_manager = 'apt-get' if platform == 'ubuntu' else 'yum'
        except Exception as e:
            self.add_message(f'Platform failed: {repr(e)}', MsgLvl.ERROR)

    def validate(self):
        cmdline_out,_,_ = self.run_cmd("cat /proc/cmdline | awk -F'console=' '{print $2}' | awk -F' ' '{print $1}'")
        if cmdline_out.strip() == "":
            self.add_message("No serial console used for kernel messages.", MsgLvl.SUCCESS)
            return

        for param in cmdline_out.split():
            parts = param.split(',', 1)
            console_name = parts[0]
            conf_rate = parts[1] if len(parts) > 1 else None

            self.add_message(f"Serial console {console_name} used for kernel messages.", MsgLvl.ERROR)

            if conf_rate and conf_rate.isdigit():
                conf_rate_int = int(conf_rate)
                if conf_rate_int < 112500:
                    self.add_message(f"Configured with baud rate {conf_rate}.", MsgLvl.ERROR)
                    self.add_message(f"Recommended to set baud rate to 112500, please replace console={console_name},{conf_rate} with console={console_name},112500.", MsgLvl.ERROR)
                else:
                    self.add_message(f"Configured with baud rate {conf_rate}.", MsgLvl.SUCCESS)
            else:
                self.add_message(f"Configured without baud rate.", MsgLvl.ERROR)

        # Unlike the deprecated nvmesh_health_check script, we don't check for stty here.

    def get_os_platform(self):
        linux_distributuion = self.node.os_info['ID'].lower()
        if linux_distributuion in ["rhel", "redhat", "centos", "ol", "rocky"]:
            return "rhel"
        elif "ubuntu" in linux_distributuion:
            return "ubuntu"
        else:
            self.add_message(f"Untested Linux distribution '{linux_distributuion}'!", MsgLvl.WARNING)

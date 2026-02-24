# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.core.entities import Service
from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl

class SystemTuning(DiagModule):
    description = "System Tuning"

    def validate(self):
        tuned_status = self.node.services['tuned'].status()

        def install_and_configure_tuned(do_install=True, do_start=True, do_policy=True):
            if do_install:
                self.run_cmd(f"sudo {self.get_package_manager()} install tuned -y", "Installing Tuned")
            if do_start:
                tuned_status.enable()
                tuned_status.start()
            if do_policy:
                self.run_cmd("tuned-adm profile latency-performance", "Setting and enabling the throughput-latency tuned policy")

        if tuned_status == Service.STATUS.NOT_FOUND:
            self.add_message("This seems to be a server without Tuned installed and running. It's highly "
                        "recommended to install and configure Tuned for best performance results!", MsgLvl.WARNING)
            self.suggest_fix("Do you want to install and configure tuned now?", False, install_and_configure_tuned)
        elif tuned_status == Service.STATUS.DOWN:
            self.add_message("Tuned service is not running! Its highly recommended to run the Tuned service", MsgLvl.WARNING)
            self.suggest_fix("Do you want to start and enable the Tuned service now?", False, install_and_configure_tuned, False)
        else:
            tuned_adm_info = self.run_cmd("sudo tuned-adm active")[0]
            self.add_message(tuned_adm_info)
            if "latency-performance" in tuned_adm_info:
                self.add_message("Tuned profile settings are OK")
            else:
                self.add_message(
                    "Tuned settings is not as recommended! Please run 'tuned-adm profile latency-performance' to set and "
                    "enable the recommended Tuned profile and verify the Tuned service is running.", MsgLvl.WARNING)
                self.suggest_fix("Do you want to set the recommended tuned parameters now?", False,
                            install_and_configure_tuned, False, False)

        irq_service = self.node.services["irqbalance"]
        if irq_service.status() == Service.STATUS.UP:
            self.add_message("The IRQ balancer is running - OK", MsgLvl.SUCCESS)
        else:
            self.add_message("The IRQ Balancer is not running. This might severely impact the system performance.", MsgLvl.WARNING)

            def enable_start_irq():
                irq_service.enable()
                irq_service.start()
            self.suggest_fix("Do you want to enable and start the IRQ Balance service now?", False, enable_start_irq)

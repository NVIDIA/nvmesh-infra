# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import re
from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl

class RNIC(DiagModule):
    description = "R-NIC"

    #TODO: add utility that will check requirements before validate, like 'lspci' vs checking with which everytime
    def validate(self):
        rnics_output = self.run_cmd("for i in $(sudo lspci | awk '/Mellanox/ {print $1}'); do echo $i; echo FW level: | tr '\n' ' ' ; sudo cat /sys/bus/pci/devices/0000:$i/infiniband/mlx*_*/fw_ver; sudo lspci -s $i -vvv | egrep -e Connect-X -e 'Product Name:' -e Subsystem -e NUMA -e LnkSta: -e LnkCap -e MaxPayload; echo ; done", is_sudo=True)[0]
        if not rnics_output:
            self.add_message("No mellanox NICs found", MsgLvl.WARNING)
            return
        rnics = rnics_output.strip().split("\n\n")
        for rnic in rnics:
            matches = dict(re.findall(r"(LnkCap|LnkSta):\s(.*)", rnic))
            capacity = dict(re.findall(r'(Speed|Width)\s*([^,(\s]*)', matches['LnkCap']))
            actual = dict(re.findall(r'(Speed|Width)\s*([^,(\s]*)', matches['LnkSta']))

            rnic_details = [l.strip() for l in rnic.splitlines()]

            if rnic_details:
                vendor = rnic_details[2].partition(':')[2].split('Device')[0].strip()
                firmware = rnic_details[1].partition(':')[2].strip()
                hca_type = None
                self.add_message(f"Checking HCA at PCIe address: {rnic_details[0]}  "
                                 f"Vendor/OEM information: {vendor}")
                try:
                    if "Product Name" in rnic_details[8]:
                        hca_type = rnic_details[8].split(':')[1].strip()
                        self.add_message(f"HCA Type: {hca_type}")
                except Exception:
                    pass
                self.add_message(f"Firmware level: {firmware}")

                if capacity['Speed'] == actual['Speed']:
                    self.add_message(
                        f"HCA PCIe speed settings OK. Running at {actual['Speed']}")
                else:
                    self.add_message(f"The HCA is capable of: {capacity['Speed']} actual speed: {actual['Speed']}\n"
                                     f"Check BIOS and HW settings to ensure max performance and a stable environment!", MsgLvl.WARNING)

                if capacity['Width'] == actual['Width']:
                    self.add_message(f"HCA PCIe width settings OK. Running at {actual['Width']}")
                else:
                    self.add_message(f"The HCA is capable of: {capacity['Width']} actual width: {actual['Width']}\n"
                                     f"Check BIOS and HW settings to ensure max performance and a stable environment!", MsgLvl.WARNING)

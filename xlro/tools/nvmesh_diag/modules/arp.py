# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl

class ARP(DiagModule):
    description = "ARP"

    def validate(self):
        if not self.get_info('host').has_root:
            self.skip('No root execution access.')
        prefixes = ["net.ipv4.conf.all.", "net.ipv4.conf.default."]
        arp_type_to_value = {"arp_filter": "1", "rp_filter": "2", "arp_ignore": "2", "arp_announce": "2"}
        incorrectly_assigned = []
        for prefix in prefixes:
            for arp_type, value in arp_type_to_value.items():
                full_type = prefix + arp_type
                out, err, code = self.node.execute("sudo sysctl -n " + full_type)
                if code:
                    self.add_message("ERROR: Cannot get value from sysctl -n %s." % full_type, MsgLvl.ERROR)
                    continue
                if value not in out:
                    self.suggest_fix(f"{full_type} set to {out.strip()}, recommended value: {value}. Would you like to set this value?",
                                False, self.run_cmd, self.node, f"sudo sysctl -w {full_type}={value}")
                    incorrectly_assigned.append(full_type)
        if incorrectly_assigned:
            self.add_message(f"Incorrectly assigned: {incorrectly_assigned}", MsgLvl.WARNING)
        else:
            self.add_message("ARP values correctly assigned.", MsgLvl.SUCCESS)

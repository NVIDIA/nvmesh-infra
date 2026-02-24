# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.core.entities import Service
from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl

class Firewall(DiagModule):
    description = "Firewall"

    def validate(self):
        if self.node.services['firewall'] == Service.STATUS.UP:
            self.add_message("Firewall active!", MsgLvl.WARNING)
            self.suggest_fix("Do you want to disable the firewall service?", False, self.node.services['firewall'].stop)
        else:
            self.add_message("Firewall Disabled", MsgLvl.SUCCESS)
        # TODO: add opening ports 4791 for roce and 27017 for mongo on management nodes

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl, DiagArgs
from datetime import datetime

def sec2str(secs):
    return f'{secs // 60}m {secs % 60}s'

class Services(DiagModule):
    modules_by_role = {
        'client': ['nvmeiba', 'nvmeibc'],
        'target': ['nvmeiba', 'nvmeibc', 'nvmeibs'],
        'manager': [],
        'mongo': [],
        'um': []  # UM doesn't use kernel modules
    }
    services_by_role = {
        'client': ['nvmeshagent', 'nvmeshclient', 'nvmeshcm',],
        'target': ['nvmeshagent', 'nvmeshclient', 'nvmeshcm', 'nvmeshtarget', 'nvmeshtoma'],
        'manager': ['nvmeshmgr'],
        'mongo': ['mongod'],
        'um': ['nvmeshagent', 'nvmeshcm', 'nvmeshum']  # UM services
    }

    services = ['nvmeshagent', 'nvmeshclient', 'nvmeshcm', 'nvmeshtarget', 'nvmeshtoma', 'nvmeshum']
    MIN_UPTIME_SECS = 600

    def discover(self):
        self.diag_info.roles = roles = []

        # Check for services directly via SSH to determine roles
        try:
            active_services = []
            cmd = "systemctl list-units --type=service --state=active --no-legend | awk '{print $1}'"
            out, _, _ = self.run_cmd(cmd)
            active_services = [svc.strip() for svc in out.split('\n') if svc.strip()]

            # Determine roles based on active services
            if any('nvmeshclient' in svc for svc in active_services):
                roles.append('client')
            if any('nvmeshtarget' in svc for svc in active_services):
                roles.append('target')
            if any('nvmeshtoma' in svc for svc in active_services):
                roles.append('target')  # TOMA is part of target role
            if any('nvmeshmgr' in svc for svc in active_services):
                roles.append('manager')
            if any('mongod' in svc for svc in active_services):
                roles.append('mongo')
            if any('nvmeshum' in svc for svc in active_services):
                roles.append('um')

            # Fall back to manager info if available and no roles detected
            if not roles and hasattr(self, 'get_info'):
                try:
                    mgr = self.get_info('cluster').manager
                    if mgr:
                        if self.node.name in mgr.mgmt_cluster:
                            roles.append('manager')
                        if self.node.name in mgr.mongo_cluster:
                            roles.append('mongo')
                        if self.node in [c.node for c in mgr.clients]:
                            roles.append('client')
                        if self.node in [t.node for t in mgr.targets]:
                            roles.append('target')
                except Exception:
                    pass
        except Exception as e:
            self.add_message(f"Failed to determine roles via SSH: {str(e)}", MsgLvl.WARNING)
            # Best guess is converged if we can't determine
            roles.extend(['target', 'client'])
            # Manager could have been given on command line
            if DiagArgs.manager and self.node.name in [mh.name for mh in DiagArgs.manager.mgmt_hosts]:
                roles.append('manager')

    def validate(self):
        # Check modules (not needed for UM nodes)
        if 'um' not in self.diag_info.roles:
            modules = {mod for role in self.diag_info.roles for mod in self.modules_by_role[role]}
            out, _, _ = self.run_cmd(f'{self.node.cache_cmd_path("lsmod")} | grep -o "^nvmeib. "')
            loaded = set(out.split())
            not_loaded = modules - loaded
            if not_loaded:
                self.add_message(f'The following kernel modules are not loaded: {not_loaded}', MsgLvl.ERROR)
            else:
                self.add_message('Kernel modules are loaded', MsgLvl.SUCCESS)
        else:
            self.add_message('Skipping kernel module check for UM nodes')

        # Check services
        services = {svc for role in self.diag_info.roles for svc in self.services_by_role[role]}
        err = False

        # Use systemctl directly to check service status
        for service in services:
            service_name = f"{service}.service"
            # Check if service is active
            active_cmd = f"systemctl is-active {service_name} || echo 'inactive'"
            active_status, _, _ = self.run_cmd(active_cmd)
            active_status = active_status.strip()

            if active_status == "active":
                # Check uptime
                uptime_cmd = f'TZ=GMT systemctl show --property=ActiveEnterTimestamp {service_name}'
                uptime_out, _, _ = self.run_cmd(uptime_cmd)
                timestamp = uptime_out.strip().split('=')[1]

                if timestamp:
                    try:
                        uptime = datetime.utcnow() - datetime.fromisoformat(timestamp[4:-4])
                        upsecs = int(uptime.total_seconds())
                        if upsecs < self.MIN_UPTIME_SECS:
                            self.add_message(f'{service} is up for less than minimum {sec2str(self.MIN_UPTIME_SECS)} '
                                         f'(Actual up time: {sec2str(upsecs)})', MsgLvl.WARNING)
                            err = True
                    except Exception as e:
                        self.add_message(f"Failed to parse timestamp for {service}: {str(e)}", MsgLvl.WARNING)
                        err = True
            else:
                # Only report error if service is expected for this role
                if service in services:
                    self.add_message(f'Service "{service}" is {active_status}', MsgLvl.ERROR)
                    err = True

        if not err:
            self.add_message(f'System services {list(services)} are running', MsgLvl.SUCCESS)

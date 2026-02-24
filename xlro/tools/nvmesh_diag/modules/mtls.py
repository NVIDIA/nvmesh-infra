# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl
import os
from typing import Set, Dict


class Mtls(DiagModule):
    description = "mTLS Configuration"

    TLS_DIR = "/etc/nvmesh/tls"
    CONFIG_FILE = "/etc/nvmesh/nvmesh.conf"

    def discover(self):
        """Discover mTLS configuration state"""
        self.diag_info.tls_dir_exists = self._exists(self.TLS_DIR, is_dir=True)
        self.diag_info.config_file_exists = self._exists(self.CONFIG_FILE)

    def validate(self):
        """Validate mTLS configuration"""
        if not self.diag_info.tls_dir_exists:
            self.add_message(f"TLS directory {self.TLS_DIR} does not exist", MsgLvl.ERROR)
            return

        # Load configuration and expectations
        config = self._load_config()
        if not config:
            return

        exp = self.expectations
        if not exp:
            self.add_message("No mTLS expectations defined", MsgLvl.WARNING)
            return

        # Run validations
        self.check_certificates(exp.get('certificates', {}), exp.get('ca_chains', []))
        self.check_keys(exp.get('required_keys', []))
        self.check_config_values(config, exp.get('required_config', {}))
        self.validate_certificates(exp.get('certificates', {}), self._extract_server_hosts(config))

    # ===== Check Methods =====

    def check_certificates(self, cert_definitions, ca_chains):
        """Check certificate existence"""
        all_certs = list(cert_definitions.keys()) + ca_chains
        missing = []

        for cert in all_certs:
            if self._exists(f"{self.TLS_DIR}/{cert}"):
                self.add_message(f"✅ Certificate {cert} exists", MsgLvl.SUCCESS)
            else:
                missing.append(cert)
                self.add_message(f"❌ Certificate {cert} missing", MsgLvl.ERROR)

        if not missing:
            self.add_message("All certificates present", MsgLvl.SUCCESS)

    def check_keys(self, required_keys):
        """Check key files existence"""
        for key_file in required_keys:
            key_path = f"{self.TLS_DIR}/{key_file}"
            if self._exists(key_path):
                self.add_message(f"✅ Key {key_file} exists", MsgLvl.SUCCESS)
            else:
                self.add_message(f"❌ Key {key_file} missing", MsgLvl.ERROR)

    def check_config_values(self, config, required_values):
        """Validate required configuration values"""
        for key, expected in required_values.items():
            actual = config.get(key)
            if actual == expected:
                self.add_message(f"✅ {key} configured correctly", MsgLvl.SUCCESS)
            elif actual:
                self.add_message(f"❌ {key}: {actual} (expected: {expected})", MsgLvl.ERROR)
            else:
                self.add_message(f"❌ {key} not configured", MsgLvl.ERROR)

    def validate_certificates(self, cert_definitions, server_hosts):
        """Validate certificate properties"""
        for cert_name, expected_ou in cert_definitions.items():
            cert_path = f"{self.TLS_DIR}/{cert_name}"
            if not self._exists(cert_path):
                continue

            # Get subject
            subject = self._get_cert_subject(cert_path)
            if not subject:
                continue

            cn = subject.get('CN', 'NOT_FOUND')
            ou = subject.get('OU', 'NOT_FOUND')
            self.add_message(f"Certificate {cert_name}: CN={cn}, OU={ou}")

            # Validate CN
            self._validate_cn(cn, server_hosts)

            # Validate OU if expected
            if expected_ou and ou != expected_ou:
                self.add_message(f"  ❌ OU mismatch: {ou} != {expected_ou}", MsgLvl.ERROR)
            elif expected_ou:
                self.add_message(f"  ✅ OU matches: {expected_ou}", MsgLvl.SUCCESS)

    # ===== Helper Methods =====

    def _exists(self, path: str, is_dir: bool = False) -> bool:
        """Check if path exists"""
        flag = "-d" if is_dir else "-f"
        _, _, code = self.run_cmd(f"test {flag} {path}", is_sudo=False, print_err=False)
        return code == 0

    def _load_config(self) -> Dict[str, str]:
        """Load configuration file"""
        if not self.diag_info.config_file_exists:
            self.add_message(f"Config file {self.CONFIG_FILE} missing", MsgLvl.ERROR)
            return {}

        out, err, code = self.run_cmd(f"cat {self.CONFIG_FILE}", is_sudo=False, print_err=False)
        if code != 0:
            self.add_message(f"Error reading config: {err.strip()}", MsgLvl.ERROR)
            return {}

        config = {}
        for line in out.split('\n'):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip().strip('"')
        return config

    def _extract_server_hosts(self, config: Dict[str, str]) -> Set[str]:
        """Extract server hosts from configuration"""
        hosts = set()
        for key in ["MANAGEMENT_SERVERS", "_REST_SERVERS", "KAFKA_SERVERS"]:
            for server in config.get(key, "").split(','):
                if ':' in server:
                    host = server.split(':')[0].strip()
                    if host:
                        hosts.add(host)
        return hosts

    def _get_cert_subject(self, cert_path: str) -> Dict[str, str]:
        """Extract certificate subject"""
        cmd = f"openssl x509 -in {cert_path} -subject -nameopt RFC2253 -noout 2>/dev/null | cut -d= -f2-"
        out, _, code = self.run_cmd(cmd, is_sudo=False, print_err=False)

        if code != 0:
            return {}

        result = {}
        for part in out.strip().split(','):
            if '=' in part:
                key, value = part.strip().split('=', 1)
                result[key] = value
        return result

    def _validate_cn(self, cn: str, server_hosts: Set[str]):
        """Validate certificate CN"""
        # Check configured servers
        if cn in server_hosts:
            self.add_message(f"  ✅ CN matches server: {cn}", MsgLvl.SUCCESS)
            return

        # Check local identity
        for cmd in ["hostname -f", "hostname -s", "hostname -I"]:
            out, _, code = self.run_cmd(cmd, is_sudo=False, print_err=False)
            if code == 0:
                if cmd == "hostname -I" and cn in out.strip().split():
                    self.add_message(f"  ✅ CN matches local IP: {cn}", MsgLvl.SUCCESS)
                    return
                elif cn == out.strip():
                    self.add_message(f"  ✅ CN matches hostname: {cn}", MsgLvl.SUCCESS)
                    return

        self.add_message(f"  ❌ CN {cn} does not match local identity or any configured server", MsgLvl.ERROR)

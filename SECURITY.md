<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Security Policy: nvmesh-infra

NVIDIA is dedicated to the security and trust of our software products and
services, including all source code repositories we manage. If you need to
report a security issue, please use the appropriate contact points below.
**Please do not report security vulnerabilities through GitHub issues,
discussions, or pull requests.**

## Reporting a Vulnerability

To report a potential security vulnerability in `nvmesh-infra`:

* **Web (preferred):** [NVIDIA Vulnerability Disclosure Program](https://www.nvidia.com/en-us/security/)
  — the preferred method for reporting security concerns across all NVIDIA products.
* **E-Mail:** [psirt@nvidia.com](mailto:psirt@nvidia.com)
  - We encourage you to use the following PGP key for secure email communication:
    [NVIDIA public PGP Key](https://www.nvidia.com/en-us/security/pgp-key)
* **GitHub:** Use the repository's **Security** tab > **Report a vulnerability**
  to submit a report privately on this repository.

If a security vulnerability is reported through public channels (issues,
discussions, or pull requests), maintainers may limit public discussion and
redirect the reporter to the private disclosure channels above.

### What to Include in Your Report

Detailed reports help NVIDIA evaluate and address issues faster. Please include:

- Product/project name and version or branch affected (e.g., `nvmesh-infra`, see `pyproject.toml`)
- Type of vulnerability (e.g., command injection, credential exposure, MITM, code execution)
- Step-by-step instructions to reproduce the issue
- Proof-of-concept code or exploit (if available)
- Potential impact assessment

### What to Expect

NVIDIA's Product Security Incident Response Team (PSIRT) will acknowledge receipt,
validate the vulnerability and assess severity, develop and test a fix, and
publish a security bulletin as appropriate. While NVIDIA does not currently
operate a bug bounty program, we offer acknowledgement when an externally
reported issue is addressed under our coordinated vulnerability disclosure
policy. For ongoing security updates, subscribe to notifications at the
[NVIDIA Product Security](https://www.nvidia.com/en-us/security/) portal, and see
the [PSIRT policies page](https://www.nvidia.com/en-us/security/psirt-policies/)
for more information.

## Security Architecture & Context

`nvmesh-infra` provides the NVMesh SDK, operator CLI toolset, and cluster
diagnostics/test infrastructure. It contains the core entity model
(`xlro/core/entities/`), a management REST client
(`xlro/core/sdk/ConnectionManager.py`), an SSH/command-execution layer
(`xlro/core/util/ssh.py`), and a family of operator CLI tools — an interactive
CLI (`xlro/tools/cli/`), `nvmesh_scanner`, `nvmesh_edit`, and the `nvmesh_diag`
diagnostics suite — routed via `xlro/core/util/router.py` and `routes.conf`. It
is a Python package (`pyproject.toml` / `poetry.lock`) that is also built into
standalone binaries with PyInstaller (`netinfo.spec`). The tools run on NVMesh
management, target, and client hosts.

This software operates at the **CLI Tool / SDK** level (a Python library plus a
set of operator command-line tools and diagnostics). Its primary security
responsibility is to authenticate to and communicate with NVMesh management
servers, execute local and remote (SSH) administrative commands across cluster
nodes, and handle operator credentials and session state on the host where it
runs.

**Repository Exposure Classification:** Public.
Basis: the project is open-source and published for public consumption
(`https://github.com/NVIDIA/nvmesh-infra`), so this document is world-readable
and is written to public-safe detail (internal hostnames, IPs, ticket IDs, and
internal tooling URLs are omitted).

**Service Exposure Classification:** External / Regulated (high confidence).
Basis: shipped as a released package / PyInstaller binary (`netinfo.spec`) that
runs on customer NVMesh storage cluster nodes as part of a commercial enterprise
product; handles management credentials, SSH keys/passwords, and privileged
(`sudo`) operations.

**Key security boundaries.** Untrusted / attacker-relevant inputs include:
responses and API/JS definitions returned by the management server over the REST
client; host names, paths, and command strings supplied to the SSH/command
layer; the content of on-host configuration and credential files (e.g.
`mgmt_creds`, session cookie jars under the local settings directory); and data
gathered by the diagnostics / log-collection modules. Trusted layers this
package delegates to include the host OS, `sudo`, the network between the tool
host and cluster nodes, and the management server itself.

### Threat Model

The following scenarios represent the primary security concerns for this project
(including auxiliary / diagnostic code), ordered roughly by severity.

1. **Remote code execution via `eval()` in the JS config parser:**
   `_parse_js_conf` in `xlro/core/util/general_utils.py` calls `eval()` on
   `Literal` raw values, unary operators, and binary expressions (lines ~705,
   711, 717) taken from JavaScript-style API/config definitions parsed with
   `pyjsparser`. If a malicious or compromised management server (reached via the
   REST client) supplies crafted definitions, expressions are evaluated in the
   operator's Python process, enabling arbitrary code execution on the admin
   host.

2. **Command injection via `shell=True` string interpolation:**
   `execute_cmd_locally`/`LocalPopen` (`xlro/core/util/ssh.py`, `subprocess`
   calls with `shell=True` at lines ~77 and ~569) run commands through a shell,
   and the `scp` helpers build command strings from host names and paths via
   f-strings (lines ~744-758). `compare_blocks.py` runs
   `subprocess.check_output("cat {} | md5sum", shell=True)` (lines ~210-211), and
   the interactive CLI executes `!`-prefixed lines with `os.system(cmd)`
   (`xlro/tools/cli/cli.py`, line ~265). Attacker-controlled host names or paths
   flowing into these can execute arbitrary shell commands.

3. **Man-in-the-middle via disabled TLS verification and HTTPS downgrade:**
   `ConnectionManager.py` sets `self.session.verify = False` (line ~222) and
   posts login credentials with `verify=False` (line ~481); TLS verification is
   only enabled when `use_tls` is explicitly configured (lines ~223-225). On an
   `SSLError`, the connection logic rewrites `https` endpoints to `http` (lines
   ~261-262). An on-path attacker between the tool host and the management server
   can intercept or modify traffic and capture the operator username/password and
   session cookie.

4. **SSH man-in-the-middle via unverified host keys:**
   `ssh.py` installs `paramiko.AutoAddPolicy()` (line ~639) and ships legacy
   defaults `StrictHostKeyChecking no` / `UserKnownHostsFile /dev/null` (line
   ~506); the `scp` helpers also pass `-o StrictHostKeyChecking=no` (lines
   ~744-758). Host keys are never validated, so an attacker who can spoof a
   target/client host can intercept SSH sessions and any credentials or commands
   sent over them.

5. **Credential and session exposure in local storage:**
   `creds.py` stores management passwords **base64-encoded (not encrypted)** in
   the `mgmt_creds` file (lines ~24, 41, 63), and `ConnectionManager.py` persists
   session cookies to a `*-cookies.txt` jar (`MozillaCookieJar`, lines ~44,
   50-51) in the local settings directory. A local user, stray backup, or
   over-broad file permission can disclose reusable cluster credentials.

6. **Sensitive data leakage through diagnostics and debug logging:**
   The `nvmesh_diag` modules (e.g. `modules/logs_collector.py`, `modules/mtls.py`
   reading NVMesh config and certificate metadata) and the opt-in
   request/command debug logging in `ConnectionManager` and `ssh.py` can bundle
   configuration, certificate metadata, and command output into collected
   artifacts. Redaction of secrets is best-effort (a field-masking helper) and
   can miss fields it does not recognize, leaking secrets into diagnostic
   bundles.

7. **Privilege escalation through `sudo` subprocess invocation:**
   `ssh.py` runs `sudo kill -9/-15` on discovered child PIDs (line ~345) and
   `temp_dir` runs `sudo rm -rf {path}` (line ~900); `nvmesh_scanner` and several
   `nvmesh_diag` modules run privileged commands with interpolated paths (e.g.
   `nvmesh_scanner.py` lines ~202, 213, 216). The tools assume `sudo` and path
   inputs are trustworthy; a crafted path or a permissive `sudoers` policy could
   be leveraged to affect unintended files or processes with elevated privileges.

### Critical Security Assumptions

- **Trusted network path:** Because TLS verification is disabled by default and
  an HTTPS→HTTP downgrade is possible, the tool assumes the network between the
  host and NVMesh management servers is trusted (or that TLS via `use_tls` is
  explicitly configured).
- **Trusted management server:** The REST client and the `_parse_js_conf`
  `eval()` path assume the management server and the API/JS definitions it
  returns are authentic and non-malicious.
- **Trusted SSH endpoints:** With `AutoAddPolicy` and no known-hosts checking,
  the tool assumes remote target/client hosts are authentic and not
  impersonated.
- **Trusted local host and files:** Credentials and cookies in the local
  settings directory are assumed to be protected by host filesystem permissions
  and a trusted local user; the package does not encrypt stored credentials.
- **Trusted operator input:** CLI arguments, host names, and file paths passed to
  the SSH/command layer are assumed to be supplied by an authorized operator and
  are not sanitized before reaching `shell=True` / `os.system` execution.
- **Host OS and `sudo` enforce privilege boundaries:** The tool relies on the OS
  and `sudoers` configuration to constrain the privileged operations it invokes.
- **Trusted, patched dependencies:** Third-party libraries (`paramiko`,
  `requests`, `urllib3`, `cryptography`, `pyjsparser`, `jinja2`, `pyyaml`) are
  assumed to be authentic and kept up to date via the pinned `poetry.lock`.

## Dependency Security

Dependencies are pinned in `pyproject.toml` / `poetry.lock`. Security-relevant
libraries include `paramiko` (SSH), `requests` / `urllib3` (HTTP), `jinja2`
(templating), `pyyaml` (config parsing), and `pyjsparser` (JS config parsing).
Keep these current and monitor advisories, as several threats above depend on
their correct behavior.

## Supported Versions

Security fixes are provided for the actively developed release line (see
`pyproject.toml`). Older packaged releases should be upgraded to receive fixes.

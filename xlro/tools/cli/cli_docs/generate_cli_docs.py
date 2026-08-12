# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
NVMesh CLI Documentation Generator - Markdown assembly from Click introspection.

This module provides ``build_document()``, which is called by the ``nvmesh generate-docs``
CLI command (see ``xlro.tools.cli.cli``). It requires a fully-populated Click command
tree, so a management connection must be established first.
"""

import datetime
from typing import List

import click


# ---------------------------------------------------------------------------
# Boilerplate sections (ported from the legacy NVMesh CLI Guide .docx)
# ---------------------------------------------------------------------------

SPDX = """\
<!--
SPDX-FileCopyrightText: Copyright (c) {year} NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->"""

COPYRIGHT = """\
## Copyright and Trademark Information

Copyright {year} NVIDIA All rights reserved.

Specifications are subject to change without notice.
NVMesh is a registered trademark of NVIDIA.
All other brands or products are trademarks or registered trademarks of their respective holders
and should be treated as such."""

PREFACE = """\
## Preface

This document describes the command-line interface of the NVMesh storage solution.
For more information on NVMesh, refer to the *NVMesh User Guide*.

**Audience** - Storage and application administration personnel responsible for
installing and deploying NVMesh."""

INTRODUCTION = """\
## Introduction

The `nvmesh` CLI tool provides a command-line user interface to manage NVMesh.
It can be used to send one-line commands or write shell scripts, and it offers
an interactive shell.

`nvmesh` uses the NVMesh RESTful API, terminal command-line tools, and SSH for
day-to-day management and provisioning activities with homogeneous semantics.

The CLI tool interacts with the Management Servers using management's REST API.
The outputs from the CLI are strongly dependent on this API.
This document is based on management **API version {api_version}**."""

INSTALLATION = """\
## Installation

Install the `nvmesh-utils` package, which should be available from the NVMesh
yum/apt repo:

```bash
sudo yum install nvmesh-utils    # RHEL/CentOS
sudo apt install nvmesh-utils    # Debian/Ubuntu
```

To start the NVMesh shell, run `nvmesh` in a terminal."""

USAGE = """\
## Using the NVMesh CLI

### Prerequisites

Two configurations should be made initially:

1. Set definitions for accessing NVMesh Management using its REST API in
   `/etc/nvmesh/nvmesh.conf`. Typically, if the NVMesh client or NVMesh target
   have been configured for this machine, these will already be in place.
   Otherwise, set `_REST_SERVERS` and `_REST_AUTH_METHOD` per the instructions
   in the file.

2. Provide login credentials. Initially, `nvmesh` has no stored credentials.
   It requires management/API login information for an administrator account.
   Upon first launch, the tool will prompt for credentials:

   ```
   Management: management-host
   User: admin
   Password:
   ```

   Credentials are stored in `~/.nvmesh/`.

### Interactive vs CLI

All capabilities are available in two modes: **Interactive** and **CLI**.

#### CLI Mode

Invoke `nvmesh` with all required parameters on a single line:

```bash
nvmesh -m management-host volume show
nvmesh -m management-host volume create --name my-vol --capacity 100G --raid-level 1
nvmesh -m management-host client attach --id client-1 --volume my-vol
```

#### Interactive Mode

Invoke `nvmesh` with no additional arguments to enter the interactive shell:

```bash
$ nvmesh -m management-host
[management-host] volume show
[management-host] volume create --name my-vol --capacity 100G --raid-level 1
```

Interactive mode features:

- Use the `!` prefix to execute shell commands locally: `! ls -l`
- Use **Tab** for auto-completion of commands and arguments
- Navigate history with **Up/Down** arrows and search with **Ctrl+R**

### Command Structure

The full command structure is obtained with `nvmesh --help`, which provides the
first level of available commands and options.

Most commands represent NVMesh entities (volumes, drives, clients, etc.).
For every entity, there is a second level of commands that are operations on
that entity - for example:

```bash
nvmesh volume --help          # List volume operations
nvmesh volume create --help   # Help for 'volume create'
```

Common operations across entities:

| Operation | Description |
|-----------|-------------|
| `show`    | List instances (with optional filters and output formats) |
| `count`   | Show how many instances exist |
| `create`  | Create a new instance |
| `update`  | Modify properties of an existing instance |
| `delete`  | Delete one or more instances |
| `wait`    | Block until a property reaches a desired value |"""


# ---------------------------------------------------------------------------
# Helpers for building the command-reference section
# ---------------------------------------------------------------------------

def opt_names(param: click.Parameter) -> str:
    return ", ".join(f"`{o}`" for o in param.opts)


def param_type_str(param: click.Parameter) -> str:
    if getattr(param, "is_flag", False):
        return "flag"
    t = param.type
    if isinstance(t, click.Choice):
        return "choice"
    name = getattr(t, "name", str(t))
    if name == "TEXT":
        return "text"
    return name.lower()


def param_choices(param: click.Parameter) -> str:
    if isinstance(param.type, click.Choice):
        return ", ".join(f"`{c}`" for c in param.type.choices)
    return ""


def param_default(param: click.Parameter) -> str:
    d = param.default
    if d is None or d == () or d is False:
        return ""
    return str(d)


def format_param_row(param: click.Parameter) -> str:
    names = opt_names(param)
    ptype = param_type_str(param)
    required = "Yes" if param.required else ""
    desc = (getattr(param, "help", None) or "").replace("|", "\\|")
    choices = param_choices(param)
    default = param_default(param)
    return f"| {names} | {ptype} | {required} | {desc} | {choices} | {default} |"


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------

def build_document(group: click.Group, version: str, api_version: str) -> str:
    year = datetime.date.today().year
    lines: List[str] = []

    # Title and SPDX
    lines.append(f"# NVMesh {version} CLI Guide")
    lines.append("")
    lines.append(SPDX.format(year=year))
    lines.append("")
    lines.append(f"API Version: **{api_version}**")
    lines.append("")

    # Boilerplate sections
    lines.append(COPYRIGHT.format(year=year))
    lines.append("")
    lines.append(PREFACE)
    lines.append("")
    lines.append(INTRODUCTION.format(api_version=api_version))
    lines.append("")
    lines.append(INSTALLATION)
    lines.append("")
    lines.append(USAGE)
    lines.append("")

    # Command Reference
    lines.append("---")
    lines.append("")
    lines.append("## Command Reference")
    lines.append("")

    # ToC
    lines.append("### Table of Contents")
    lines.append("")
    for cmd_name in sorted(group.commands):
        cmd = group.commands[cmd_name]
        anchor = cmd_name
        lines.append(f"- [{cmd_name}](#{anchor})")
        if isinstance(cmd, click.Group):
            for sub_name in sorted(cmd.commands):
                sub_anchor = f"{cmd_name}-{sub_name}"
                lines.append(f"  - [{cmd_name} {sub_name}](#{sub_anchor})")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Command details
    for cmd_name in sorted(group.commands):
        cmd = group.commands[cmd_name]
        lines.append(f"### {cmd_name}")
        lines.append("")

        if cmd.help:
            lines.append(f"_{cmd.get_short_help_str(limit=300)}_")
            lines.append("")

        if not isinstance(cmd, click.Group):
            params = [p for p in cmd.params if p.name not in ("help",)]
            if params:
                lines.append("| Argument | Type | Required | Description | Choices | Default |")
                lines.append("|----------|------|----------|-------------|---------|---------|")
                for p in params:
                    lines.append(format_param_row(p))
                lines.append("")
            lines.append("---")
            lines.append("")
            continue

        for sub_name in sorted(cmd.commands):
            sub = cmd.commands[sub_name]
            lines.append(f"#### {cmd_name} {sub_name}")
            lines.append("")

            if sub.help:
                lines.append(f"_{sub.get_short_help_str(limit=300)}_")
                lines.append("")

            params = [p for p in sub.params if p.name not in ("help",)]
            if params:
                lines.append("| Argument | Type | Required | Description | Choices | Default |")
                lines.append("|----------|------|----------|-------------|---------|---------|")
                for p in params:
                    lines.append(format_param_row(p))
                lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)

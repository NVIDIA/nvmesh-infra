# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

Monitors
--------
    A monitor is a thread that observes something, checking for events of interest.
    Most tests poll all monitors to check for error or change events.
    Monitors come in several flavors:
        Basic command Monitors run a long-running process somewhere and scan the output text
        for regex or other patterns.  The two prime examples are IO Monitors, which spawn
        a long-running I/O and watch for errors/panics, and Journal monitors which follow
        the journalctl looking for error conditions.

        Polling Monitors periodically run a short-running command and the output (or exit code)
        is scanned for error events. An example would be a proc-file monitor looking for error
        indicators.

        Watcher Monitors run a long-running "watcher", which periodically runs some short-running
        command and diffs the output with the prior run.  An example would be a "service show" command
        which shows which services are active.  A watcher can also watch a file or directory for
        changes, for example a Core Watcher checking for presence of core files.

    Monitors accumulate "events" which are just dictionaries containing event specific content and
    control information, i.e., event ID, type of event, which monitor generated the event, on which host, etc.

    Monitors can be grouped in a hierarchy, so a parent monitor can be polled and will reflect the
    events of it's children.

    Monitors can be manually stopped, restarted or set to auto-restart.

    Monitors can be specified by a YAML file vs. code.  See examples.yaml and the monitors section of
    infra_config.yaml

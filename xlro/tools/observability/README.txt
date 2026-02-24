# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

Observability staging cluster:
=================================

Currently resides on nvme142, observability staging cluster consists of multiple building blocks that should simulate the observability ecosystem in NVIDIA.


1) Prometheus (Port 9090) - Collect metrics from targets running the nvmeshexporter service. You can add targets by changing prometheus.yml file and restarting prometheus.

2) Opensearch (Listener on port 9200, Dashboard on port 5601) - Collect logs and events configured on the nodes via fluent-bit to the nvmesh* index.

3) Neo4J (Port 7474) - A graph DB for querying changes in relations of nvmesh entities (Ongoing)

4) Graphana (Port 3000) - Uses the mentioned above components as data sources to create observability dashboards


Prerequisites:
===============

Make sure you have the following docker volumes:

[jenkins@nvme142 compose-demo]$ docker volume ls
DRIVER              VOLUME NAME
local               grafana-storage
local               neo4j-storage
local               opensearch-storage
local               prometheus-storage


How to run:
=============
In the observability directory you can perform the following operations:

1) Check if cluster is up -

[jenkins@nvme142 compose-demo]$ docker-compose ps
               Name                              Command               State                                 Ports
-------------------------------------------------------------------------------------------------------------------------------------------------
observability-grafana                 /run.sh                          Up      0.0.0.0:3000->3000/tcp
observability-neo4j                   tini -g -- /startup/docker ...   Up      7473/tcp, 0.0.0.0:7474->7474/tcp, 0.0.0.0:7687->7687/tcp
observability-opensearch              ./opensearch-docker-entryp ...   Up      0.0.0.0:9200->9200/tcp, 9300/tcp, 0.0.0.0:9600->9600/tcp, 9650/tcp
observability-opensearch-dashboards   ./opensearch-dashboards-do ...   Up      0.0.0.0:5601->5601/tcp
observability-prometheus              /bin/prometheus --config.f ...   Up      0.0.0.0:9090->9090/tcp

2) Stop/Start/Restart services - use docker-compose [start|stop|restart]

3) Shutdown containers -

[jenkins@nvme142 compose-demo]$ docker-compose down
Stopping observability-opensearch            ... done
Stopping observability-neo4j                 ... done
Stopping observability-prometheus            ... done
Stopping observability-opensearch-dashboards ... done
Stopping observability-grafana               ... done
Removing observability-opensearch            ... done
Removing observability-neo4j                 ... done
Removing observability-prometheus            ... done
Removing observability-opensearch-dashboards ... done
Removing observability-grafana               ... done
Removing network compose-demo_default

4) Start containers -

[jenkins@nvme142 compose-demo]$ docker-compose up -d
Creating network "compose-demo_default" with the default driver
Creating observability-neo4j                 ... done
Creating observability-grafana               ... done
Creating observability-opensearch            ... done
Creating observability-opensearch-dashboards ... done
Creating observability-prometheus            ... done


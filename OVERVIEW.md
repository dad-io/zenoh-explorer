## Overview

This is a Zenoh-based peer & client messaging utility with file transfer features:

- **Subscribe** to key expressions and monitor real-time Zenoh data flows
- **Publish** data to the network for testing and debugging, including large files of any type
- **Query** specific data from the Zenoh network, or enable your queryable to serve requests
- **Browse** the Zenoh network data topology in real-time
- **Monitor** all Zenoh network messaging activity with filtering and search capabilities

### Highlights

- **Large File Transfers**: Publish files up to 4GB as single payloads. Files larger than 4GB are automatically chunked (64MB chunks) for seamless transmission. Network configuration supports messages up to 100GB.
- **Built-in Query Service**: Enable a simple queryable service to respond to network queries using locally stored data. Test request/response patterns across remote keyspaces without deploying separate services—ideal for development and debugging.
- **Real-time Network Monitoring**: Automatically monitors all Zenoh network traffic via a dedicated session subscribed to `**`.

---

## Quickstart

### Getting Started

1. Configure connection settings and click **Connect**
   - For a quick peer mesh, leave as **Peer** with the **Address** field blank and select the tcp port of your peers (7447 by default)
   - *EARLY VERSION: Only tcp transport and multicast have been tested*
2. Use **Subscribe** tab to listen to key expressions (e.g., `demo/**`)
3. Use **Publish** tab to send data. Enter text or import files of any size or type.
4. Use **Browse** tab to explore the keyspace tree and see live updates
5. Use **Messages** tab to see all messaging activity
6. Enable simple **Queryables** service (optional, respond to queries for items in keyspace)

### Connection Modes

- **Client Mode**: Connect to Zenoh routers
- **Peer Mode**: Participate as a peer in a mesh network (*EARLY VERSION: requires multicast & open firewalls*)

### Key Expression Examples

| Pattern | Description |
|---------|-------------|
| `**` | Match all keys |
| `demo/**` | Match all keys under demo/ |
| `sensor/*/temperature` | Match temperature under any sensor |
| `device/1/status` | Match exact key |

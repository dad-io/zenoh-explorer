# Zenoh Explorer
A GUI application for exploring, debugging, and monitoring Zenoh networks.
![Zenoh Explorer](ze-screenshot.png)

```
          ╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮
          │                                                                        │
          │         ███████╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗                    │
          │         ╚══███╔╝██╔════╝████╗  ██║██╔═══██╗██║  ██║                    │
          │           ███╔╝ █████╗  ██╔██╗ ██║██║   ██║███████║                    │
          │          ███╔╝  ██╔══╝  ██║╚██╗██║██║   ██║██╔══██║                    │
          │         ███████╗███████╗██║ ╚████║╚██████╔╝██║  ██║                    │
          │         ╚══════╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝                    │
          │    ███████╗██╗  ██╗██████╗ ██╗      ██████╗ ██████╗ ███████╗██████╗    │
          │    ██╔════╝╚██╗██╔╝██╔══██╗██║     ██╔═══██╗██╔══██╗██╔════╝██╔══██╗   │
          │    █████╗   ╚███╔╝ ██████╔╝██║     ██║   ██║██████╔╝█████╗  ██████╔╝   │
          │    ██╔══╝   ██╔██╗ ██╔═══╝ ██║     ██║   ██║██╔══██╗██╔══╝  ██╔══██╗   │
          │    ███████╗██╔╝ ██╗██║     ███████╗╚██████╔╝██║  ██║███████╗██║  ██║   │
          │    ╚══════╝╚═╝  ╚═╝╚═╝     ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝   │
          │                                                                        │
          ╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯
```
## Features
- **Real-time Network Monitoring**: View all messages flowing through the Zenoh network
- **Interactive Subscriptions**: Subscribe to key expressions with wildcards and patterns
- **Data Publishing**: Send test data to any key in the network
  - Support for different encodings
  - **File import**: Import a file and publish it (the whole file is read into memory; payloads above 64 MiB are sent in chunks)
  - **Built-in queryable** (Publish tab): answers queries with the last value this app published on each key (typed text only, up to 10 MB; not imported files)
- **Query Interface**: Request data from the network with a configurable timeout (default: 10 seconds)
  - Test request/response patterns against the built-in queryable without external services
- **Topic Browser**: Explore the hierarchical structure of keys and data
  - Tree-based visualization
  - Shows last received payload and message count
  - Auto-expanding navigation

### Connection Options
- **Client Mode**: Connect as a Zenoh client to existing routers
- **Peer Mode**: Participate as a peer in the mesh network
- **Flexible Locators**: Support for TCP, UDP, and other transport protocols

## Installation

### Download a release

Each [GitHub release](https://github.com/dad-io/zenoh-explorer/releases) has these assets:

| Platform | Asset |
|---|---|
| macOS, Apple silicon | `zenoh-explorer-aarch64-apple-darwin.zip` (contains `Zenoh Explorer.app`) |
| macOS, Intel | `zenoh-explorer-x86_64-apple-darwin.zip` (contains `Zenoh Explorer.app`) |
| Windows x64 | `zenoh-explorer-x86_64-pc-windows-msvc.zip` |
| Linux x64 | `zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz` |
| Linux arm64 | `zenoh-explorer-aarch64-unknown-linux-gnu.tar.gz` |

`checksums-sha256.txt` lists the SHA-256 of every asset. The `*-debug-symbols.*` assets are only needed to symbolize crash backtraces.

**Verify the checksum** (macOS/Linux) before running a download:

```bash
grep ' zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz$' checksums-sha256.txt | shasum -a 256 -c -
```

On Windows (PowerShell), compare the output with the line in `checksums-sha256.txt`:

```powershell
(Get-FileHash .\zenoh-explorer-x86_64-pc-windows-msvc.zip -Algorithm SHA256).Hash.ToLower()
```

**Verify the build provenance** (optional, needs the [GitHub CLI](https://cli.github.com/); attestations exist only for releases built from a public repository):

```bash
gh attestation verify zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz --repo dad-io/zenoh-explorer
```

**Unsigned builds.** Releases are code-signed only when the maintainers' signing credentials are configured. For an unsigned build:
- macOS: Gatekeeper blocks the first launch. Right-click `Zenoh Explorer.app` → Open, or run `xattr -d com.apple.quarantine "Zenoh Explorer.app"`.
- Windows: SmartScreen shows "Windows protected your PC". Choose More info → Run anyway.

### Building from source

Prerequisites: Rust 1.88 or later.

```bash
git clone https://github.com/dad-io/zenoh-explorer.git
cd zenoh-explorer
cargo build --release --locked
```

On macOS, `scripts/bundle-macos.sh` wraps the built binary into `target/Zenoh Explorer.app`.

## Common Usage
- Start with `demo/**` to test basic connectivity
- Use the publish tab to send test messages
- Monitor the messages tab to verify data flow
- Check the Topics tab to understand network structure
- Use Subscribe for continuous data monitoring, Query for on-demand data requests

### Key Expression Examples

- `demo/**` - Match all keys under the demo namespace
- `sensor/*/temperature` - Match temperature readings from any sensor
- `device/1/status` - Match the exact status key for device 1
- `telemetry/**/cpu` - Match CPU metrics at any depth under telemetry

## Troubleshooting

### Connecting
- **Peer mode** (the default) finds other peers on the local network by multicast (UDP 7446). Leave the address empty for multicast discovery, or give an endpoint in `tcp/ip:port` form. Use a different Listen Port for each copy of the app on one machine.
- **Client mode** connects to a router and needs its address (for example `localhost`, port 7447).
- **Connection Retry Behavior**: When you specify a TCP locator in peer mode (e.g., `tcp/localhost:7447`), Zenoh will continuously attempt to connect to that endpoint with exponential backoff. This is normal behavior - Zenoh peers persistently try to establish connections to configured endpoints, even if they're unreachable. The retry period starts at 1 second and increases (1s, 2s, 4s, 4s...) up to a maximum period. This ensures peers can automatically reconnect when endpoints become available.
- **Connection error**: the red message in the connection panel, above the Connect button, names the cause; the header shows its first words.
- **Connected but the tree stays empty**: check the peer count in the header ("no peers" means no Zenoh peer or router is linked to this app). If the header says "monitor off", subscribe to a key expression such as `demo/**`.
- For more detail, run the app with `RUST_LOG=zenoh_explorer=info`.

### Query Functionality
- Queries need a queryable on the network whose key expression matches.
- A query that says "No replies" matched no queryable, or the ones that matched had nothing to return. A timeout with no replies is shown as an error.
- **Built-in queryable**: in the Publish tab, turn on Enable Queryable. This app then answers queries with the last value it published on each key (typed text only, up to 10 MB; not imported files).

### Performance Tips
- Use specific key expressions instead of broad wildcards when possible
- Clear message history periodically for long-running sessions
- Enable debug logging sparingly: `RUST_LOG=zenoh_explorer=debug`

## Contributing

This is a standalone Zenoh network explorer designed to be a generic debugging and monitoring tool. Contributions are welcome for:

- Anything
- Proactive peer / router topic tree browsing
- Improved network / peer and router UX
- Testing
- Additional transport protocol support

## License

Apache-2.0

## Related Projects

- [Zenoh](https://zenoh.io/): The core Zenoh protocol and implementations
- [Zenoh Python](https://github.com/eclipse-zenoh/zenoh-python): Python bindings for Zenoh
- [Zenoh C](https://github.com/eclipse-zenoh/zenoh-c): C/C++ bindings for Zenoh



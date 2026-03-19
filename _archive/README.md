# Zenoh Explorer

```
    ╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮
    │                                                                          │
    │    ███████╗███████╗███╗   ██╗ ██████╗ ██╗  ██╗                         │
    │    ╚══███╔╝██╔════╝████╗  ██║██╔═══██╗██║  ██║                         │
    │      ███╔╝ █████╗  ██╔██╗ ██║██║   ██║███████║                         │
    │     ███╔╝  ██╔══╝  ██║╚██╗██║██║   ██║██╔══██║                         │
    │    ███████╗███████╗██║ ╚████║╚██████╔╝██║  ██║                         │
    │    ╚══════╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═╝                         │
    │    ███████╗██╗  ██╗██████╗ ██╗      ██████╗ ██████╗ ███████╗██████╗   │
    │    ██╔════╝╚██╗██╔╝██╔══██╗██║     ██╔═══██╗██╔══██╗██╔════╝██╔══██╗  │
    │    █████╗   ╚███╔╝ ██████╔╝██║     ██║   ██║██████╔╝█████╗  ██████╔╝  │
    │    ██╔══╝   ██╔██╗ ██╔═══╝ ██║     ██║   ██║██╔══██╗██╔══╝  ██╔══██╗  │
    │    ███████╗██╔╝ ██╗██║     ███████╗╚██████╔╝██║  ██║███████╗██║  ██║  │
    │    ╚══════╝╚═╝  ╚═╝╚═╝     ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝  │
    │                                                                          │
    │                            ,  ,                                          │
    │                           / \/ \              🌐 Network Explorer        │
    │                          (/ //_ \_                                       │
    │                           \||  .  \          📡 Real-time Monitor        │
    │                            \\___-._)                                     │
    │                         .---''---...__        🔍 Debug & Query           │
    │                       ,' . . . . . .\_\                                  │
    │                      /  /~~\ / ~~\. ~~|      ⚡ Distributed Systems      │
    │                     / ./\  /  /\_\  \_|                                  │
    │                    (  //)(/  (/ \/) //)         The Zenoh Dragon        │
    │                     '////    '===='//                                    │
    │                                                                          │
    ╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯
```

A standalone native GUI application for exploring, debugging, and monitoring Zenoh networks.

## Highlights

- **Large File Transfers**: Publish files up to 4GB as single payloads. Files larger than 4GB are automatically chunked (64MB chunks) for seamless transmission. Network configuration supports messages up to 100GB.
- **Built-in Query Service**: Enable a simple queryable service to respond to network queries using locally stored data. Test request/response patterns across remote keyspaces without deploying separate services—ideal for development and debugging.
- **Real-time Network Monitoring**: Automatically monitors all network traffic via a dedicated session subscribed to `**`.

## Overview

Zenoh Explorer is a network debugging tool built specifically for Zenoh networks. It provides a graphical interface to:

- **Subscribe** to key expressions and monitor real-time data flows
- **Publish** data to the network for testing and debugging, including large binary files
- **Query** specific data from the network, or enable a queryable to serve requests
- **Browse** the network data topology in real-time
- **Monitor** all network activity with filtering and search capabilities

## Features

### Core Functionality
- **Real-time Network Monitoring**: View all messages flowing through the Zenoh network
  - Configurable memory and message limits
  - Rate limiting to prevent UI flooding (configurable 10-10000 msg/s)
  - Accurate memory usage tracking
- **Interactive Subscriptions**: Subscribe to key expressions with wildcards and patterns
  - Multiple concurrent subscriptions
  - Clean cancellation support
  - Visual subscription status
- **Data Publishing**: Send test data to any key in the network
  - Support for different encodings
  - **File import support**: Import and publish binary files directly
  - **Large payload support**: Up to 4GB single payloads, automatic chunking for larger files
  - Immediate visual feedback
- **Query Interface**: Request data from the network with configurable timeouts
  - Configurable timeout (default: 5 seconds)
  - **Built-in queryable service**: Enable a queryable to respond to queries using locally published data
  - Test request/response patterns without external services
- **Network Browser**: Explore the hierarchical structure of keys and data
  - Tree-based visualization
  - Shows last received payload and message count
  - Auto-expanding navigation

### User Interface
- **Native Rust GUI**: Built with egui for native performance
- **Dark/Light Mode**: Toggle between themes for comfortable viewing
  - Optimized color schemes for readability
  - Consistent visual hierarchy
- **Message Filtering**: Search and filter network messages in real-time
- **Auto-scroll**: Automatically follow new messages as they arrive
- **Tabbed Interface**: Organized workflow with dedicated tabs for each function
- **Health Monitoring**: Visual indicators for connection and worker status
  - Connection status badge
  - Worker health indicator (green = healthy, yellow = unresponsive)
- **Performance Controls**: Adjustable limits directly from the UI
  - Memory limit slider (10-1000 MB)
  - Message count slider (100-50000)
  - Rate limit slider (10-10000 msg/s)

### Connection Options
- **Client Mode**: Connect as a Zenoh client to existing routers
- **Peer Mode**: Participate as a peer in the mesh network
- **Flexible Locators**: Support for TCP, UDP, and other transport protocols
- **Custom Configuration**: Advanced Zenoh configuration via JSON

## Installation

### Building from Source

### Prerequisites
- Rust 1.70 or later

```bash
git clone <repository-url>
cd zenoh-explorer
cargo build --release
```

### Running

```bash
cargo run --release
```

The application will start with a default configuration connecting to `tcp/localhost:7447`.

## Usage

### Getting Started

1. **Launch the Application**: Run the executable or use `cargo run`
2. **Configure Connection**:
   - Leave the locator/connection settings as their defaults
   - Choose Peer connection mode 
   - Click "Connect"
3. **Start Exploring**: Use the tabs to subscribe, publish, query, or browse the network

### Tabs Overview

#### Subscribe Tab
- Enter key expressions to listen for data (e.g., `sensors/**`, `device/*/status`)
- View active subscriptions
- Unsubscribe from specific key expressions

#### Publish Tab
- Send data to any key in the network
- Specify payload content and encoding
- **Import files**: Click "Import File" to publish binary files (up to 4GB single payload, automatic chunking for larger)
- Test network connectivity and data flow

#### Query Tab
- Request data from specific selectors
- Set custom timeout values
- Include optional query payloads
- **Enable Queryable**: Toggle on to make this instance respond to queries using locally published data

#### Browse Tab
- Explore the network's hierarchical key structure
- See recently received data for each key
- Expand/collapse tree nodes for navigation

#### Messages Tab
- View all network activity in chronological order
- Filter messages by key or content
- Clear message history
- Toggle auto-scroll for real-time monitoring

#### Help Tab
- Quick reference for key expression patterns
- Getting started guide
- Usage examples

### Key Expression Examples

- `demo/**` - Match all keys under the demo namespace
- `sensor/*/temperature` - Match temperature readings from any sensor
- `device/1/status` - Match the exact status key for device 1
- `telemetry/**/cpu` - Match CPU metrics at any depth under telemetry

### Container

```
cd zenoh-explorer
docker build -t zenoh-explorer .
docker run zenoh-explorer
```

## Architecture

Zenoh Explorer is built with:
- **egui/eframe**: Native GUI framework for responsive, cross-platform interfaces
- **Zenoh 1.0**: Latest Zenoh protocol implementation with unstable features
- **Tokio**: Async runtime for handling network operations
- **Chrono**: Time handling for message timestamps
- **Tracing**: Structured logging for debugging

The application uses a dual-thread architecture:
- **Main Thread**:
  - GUI rendering and user interaction
  - Message history management
  - Worker health monitoring
- **Worker Thread**:
  - Async Zenoh operations
  - Subscription management
  - Network communication
- **Communication**:
  - `std::sync::mpsc` channels for thread-safe messaging
  - Command/Event pattern for clean separation
  - Health check ping/pong for liveness detection

## Troubleshooting

### Connection Issues
- Verify the Zenoh router is running and accessible
- Check firewall settings for the specified ports
- Try connecting in peer mode if client mode fails
- Ensure the locator format is correct

### Peer Mode Configuration
- **For multicast discovery**: Leave locators empty in peer mode
- **For specific endpoints**: Provide tcp/ip:port format
- Peer mode enables automatic discovery of other peers via multicast
- **Connection Retry Behavior**: When you specify a TCP locator in peer mode (e.g., `tcp/localhost:7447`), Zenoh will continuously attempt to connect to that endpoint with exponential backoff. This is normal behavior - Zenoh peers persistently try to establish connections to configured endpoints, even if they're unreachable. The retry period starts at 1 second and increases (1s, 2s, 4s, 4s...) up to a maximum period. This ensures peers can automatically reconnect when endpoints become available.
- If peer mode shows "Worker Unresponsive", check logs with RUST_LOG=zenoh_explorer=info

### Query Functionality
- Queries require queryable services to be running on the network
- If you receive "No queryables available" alerts, it means no services are responding to your query
- **Enable the built-in queryable**: In the Query tab, enable the queryable toggle to make this instance respond to queries using locally stored data (from previous publishes)
- This lets you test request/response patterns across a network without deploying separate services
- Queryables are different from publishers - they actively respond to query requests
- For passive data monitoring, use the Subscribe tab instead of Query

### UI Issues
- The application automatically enables/disables buttons based on connection status
- Buttons are only active when connected to a Zenoh network

### Performance Tips
- Use specific key expressions instead of broad wildcards when possible
- Clear message history periodically for long-running sessions
- Adjust performance limits using the sliders in the Messages tab:
  - Lower memory limit for resource-constrained systems
  - Higher rate limit for high-traffic monitoring
  - Balance message count vs memory usage
- Enable debug logging sparingly: `RUST_LOG=zenoh_explorer=debug`
- Use release builds for production: `cargo build --release`

### Common Patterns
- Start with `demo/**` to test basic connectivity
- Use the publish tab to send test messages
- Monitor the messages tab to verify data flow
- Check the browse tab to understand network structure
- Use Subscribe for continuous data monitoring, Query for on-demand data requests

## Contributing

This is a standalone Zenoh network explorer designed to be a generic debugging and monitoring tool. Contributions are welcome for:

- Additional transport protocol support
- Enhanced filtering and search capabilities
- Performance optimizations
- UI/UX improvements
- Documentation updates

## License

Apache-2.0

## Related Projects

- [Zenoh](https://zenoh.io/): The core Zenoh protocol and implementations
- [Zenoh Python](https://github.com/eclipse-zenoh/zenoh-python): Python bindings for Zenoh
- [Zenoh C](https://github.com/eclipse-zenoh/zenoh-c): C/C++ bindings for Zenoh

## Recent Updates

- **Large File Transfer Support**: Publish files up to 4GB as single payloads, with automatic 64MB chunking for larger files
- **Built-in Queryable Service**: Enable a queryable to respond to queries across the network using locally published data
- **File Import**: Import binary files directly from the Publish tab
- **Memory Management**: Accurate memory tracking with configurable limits
- **Rate Limiting**: Token bucket algorithm to prevent message flooding
- **Worker Health Monitoring**: Visual indicators for system responsiveness
- **Enhanced UI**: Improved color schemes and button visibility
- **Performance Controls**: Real-time adjustment of limits via UI sliders
- **Better Error Messages**: More informative connection and query feedback

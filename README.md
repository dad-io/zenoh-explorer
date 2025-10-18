# Zenoh Explorer

A standalone native GUI application for exploring, debugging, and monitoring Zenoh networks.

## Overview

Zenoh Explorer is a network debugging tool built specifically for Zenoh networks. It provides a graphical interface to:

- **Subscribe** to key expressions and monitor real-time data flows
- **Publish** data to the network for testing and debugging
- TODO: **Query** specific data from the network
- PARTIAL: **Browse** the network topology in real-time
- TODO: **Monitor** all network activity with filtering and search capabilities

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
  - Immediate visual feedback
- **Query Interface**: Request data from the network with configurable timeouts
  - Note: Requires queryable services on the network
  - Configurable timeout (default: 5 seconds)
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

### Prerequisites
- Rust 1.70 or later
- A Zenoh router or network to connect to

### Building from Source

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
   - Set the locator (e.g., `tcp/192.168.1.100:7447`)
   - Choose connection mode (Client or Peer)
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
- Test network connectivity and data flow

#### Query Tab
- Request data from specific selectors
- Set custom timeout values
- Include optional query payloads

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

## Configuration

### Connection Settings
- **Locators**: Specify one or more Zenoh endpoints (comma-separated)
- **Mode**: Choose between Client (connects to routers) or Peer (mesh participant)
- **Advanced Config**: Provide custom Zenoh configuration as JSON

### Common Locator Formats
- `tcp/localhost:7447` - Local TCP connection
- `tcp/192.168.1.100:7447` - Remote TCP connection
- `udp/224.0.0.224:7447` - UDP multicast
- `quic/192.168.1.100:7447` - QUIC transport

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
- If peer mode shows "Worker Unresponsive", check logs with RUST_LOG=zenoh_explorer=info

### Query Functionality
- **Important**: Queries require queryable services to be running on the network
- If you receive "No queryables available" alerts, it means no services are responding to your query
- For passive data monitoring, use the Subscribe tab instead of Query
- Queryables are different from publishers - they actively respond to query requests

### UI Issues
- If buttons appear invisible but functional, try toggling between light/dark mode
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

- **Memory Management**: Accurate memory tracking with configurable limits
- **Rate Limiting**: Token bucket algorithm to prevent message flooding
- **Worker Health Monitoring**: Visual indicators for system responsiveness
- **Enhanced UI**: Improved color schemes and button visibility
- **Performance Controls**: Real-time adjustment of limits via UI sliders
- **Better Error Messages**: More informative connection and query feedback

# Peer Mode Connection Debug

## Changes Made

I've added comprehensive logging to help debug the peer mode connection issue where the UI shows "Connecting..." but never transitions to "Connected".

### 1. Command Flow Logging
- Added logging when GUI sends Connect command (line 1136)
- Added logging when worker receives command (line 658)
- Added logging for Connect command processing (line 665)

### 2. Event Flow Logging
- Added logging when worker sends Connected event (line 671)
- Added logging when GUI receives Connected event (line 509)
- Added logging for event processing (line 498)

### 3. Connection Process Logging
- Added detailed logging in connect_zenoh function (lines 959-964)
- Shows mode, endpoints, and multicast settings
- Added success/failure logging with mode info (lines 972-981)

### 4. Performance Improvements
- Changed worker thread from busy-waiting with try_recv to recv_timeout (line 656)
- This reduces CPU usage and improves responsiveness
- Added proper error handling for disconnected channels

### 5. Peer Mode Stabilization
- Added 500ms delay after peer mode connection (line 983)
- This gives the session time to stabilize before reporting success

## How to Debug

1. Run with debug logging enabled:
   ```bash
   RUST_LOG=debug cargo run --release
   ```

2. Try connecting in peer mode and watch the logs for:
   - "GUI sending Connect command"
   - "Worker received command"
   - "Worker processing connect command"
   - "Opening Zenoh session with mode"
   - "Successfully connected to Zenoh network"
   - "Successfully sent Connected event to GUI"
   - "GUI received Connected event"

3. If connection fails, look for:
   - "Failed to connect in peer mode"
   - "Connection timeout in peer mode"
   - "Failed to send Connected event"

## Expected Log Flow

For a successful peer mode connection:
1. GUI sending Connect command - mode: peer, locators: (empty or specified)
2. Worker received command: Connect
3. Worker processing connect command - mode: peer, locators: ...
4. Opening Zenoh session with mode: "peer"
5. Peer mode - multicast enabled: Some(true)
6. Starting Zenoh session open...
7. Successfully connected to Zenoh network in peer mode
8. Peer mode: waiting for session to stabilize...
9. Successfully sent Connected event to GUI
10. GUI received Connected event

## Potential Issues

1. **Channel Communication**: If events aren't being received, check for "Failed to send Connected event"
2. **Timeout**: Peer mode might take longer to connect, especially with multicast discovery
3. **Network Issues**: Ensure multicast is not blocked by firewall (port 7446)
4. **Event Processing**: Make sure the GUI's process_events is being called regularly

## Next Steps

If the issue persists after these changes:
1. Check if the worker thread is healthy (look for health check pings/pongs)
2. Verify multicast is working on your network
3. Try with explicit endpoints instead of multicast discovery
4. Check if any firewall rules are blocking Zenoh ports
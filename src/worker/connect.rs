//! Opening the publishing and monitor Zenoh sessions.

use tracing::{debug, error, info};
use zenoh::config::WhatAmI;
use zenoh::Session;

/// The publishing session's listen port. Port 0 is refused: zenoh would pick a
/// port at open time, and the monitor could not dial it.
pub(crate) fn parse_listen_port(listen_port: &str) -> Result<u16, String> {
    match listen_port.trim().parse::<u16>() {
        Ok(0) | Err(_) => Err(format!(
            "invalid listen port '{listen_port}': use 1 to 65535"
        )),
        Ok(p) => Ok(p),
    }
}

/// Drop zenoh's " at <source path>:<line>." suffixes and lead with advice the
/// user can act on.
pub(crate) fn user_error(mode: &str, locators: &str, raw: &str) -> String {
    let clean = crate::validation::strip_source_path(raw)
        .trim()
        .trim_end_matches('!')
        .trim()
        .to_string();
    if mode == "client" && locators.is_empty() {
        return format!(
            "Client mode needs a router address. Enter one, or switch Mode to Peer. ({clean})"
        );
    }
    format!("Could not connect in {mode} mode: {clean}")
}

/// The transport the publishing session listens on: the first locator's, or tcp.
fn listen_protocol(locators: &str) -> &str {
    match locators.split(',').next().unwrap_or("").trim() {
        "" => "tcp",
        first => first.split('/').next().unwrap_or("tcp"),
    }
}

/// Endpoints for the client-mode monitor. A peer-mode publishing session is dialled
/// on its own `[::]` listener through both loopbacks, IPv6 first. A client-mode one
/// shares its routers.
pub(crate) fn monitor_endpoints(
    locators: &str,
    listen_port: &str,
    mode: &str,
) -> Result<Vec<String>, String> {
    if mode == "client" {
        let routers: Vec<String> = locators
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(String::from)
            .collect();
        if routers.is_empty() {
            return Err("client mode needs a router address".into());
        }
        return Ok(routers);
    }
    let port = parse_listen_port(listen_port)?;
    let proto = listen_protocol(locators);
    Ok(vec![
        format!("{proto}/[::1]:{port}"),
        format!("{proto}/127.0.0.1:{port}"),
    ])
}

/// Establishes a connection to the Zenoh network with the specified configuration.
///
/// # Arguments
/// * `locators` - Comma-separated list of endpoints (e.g., "tcp/localhost:7447")
/// * `mode` - Connection mode: "client" or "peer"
/// * `config_json` - Additional Zenoh configuration in JSON format
///
/// # Returns
/// A Zenoh session on success, or an error if connection fails
pub async fn connect_zenoh(
    locators: &str,
    listen_port: &str,
    mode: &str,
    config_json: &str,
) -> Result<Session, Box<dyn std::error::Error + Send + Sync>> {
    info!(
        "Attempting to connect - mode: {}, locators: {}, listen_port: {}",
        mode,
        if locators.is_empty() {
            "(none - using discovery)"
        } else {
            locators
        },
        listen_port
    );

    let mut config = zenoh::config::Config::default();

    // Protocol of the listen endpoint: the first locator's, or tcp when
    // locators is empty (multicast discovery mode)
    let protocol = listen_protocol(locators);
    let is_udp_based = protocol == "udp" || protocol == "quic";

    // Set batch size based on protocol
    // UDP: Use MTU-safe size (1472 = 1500 - 20 IP - 8 UDP headers) to avoid IP fragmentation
    // TCP: Use maximum (65535) since TCP handles segmentation reliably
    let batch_size: u16 = if is_udp_based { 1472 } else { 65535 };
    config
        .transport
        .link
        .tx
        .set_batch_size(batch_size)
        .map_err(|e| format!("config error: {e:?}"))?;

    // Increase RX buffer size for handling high-throughput (default is 65535)
    // Set to 16MB to handle large fragmented messages over UDP
    config
        .transport
        .link
        .rx
        .set_buffer_size(16 * 1024 * 1024)
        .map_err(|e| format!("config error: {e:?}"))?;

    info!(
        "Set batch_size to {} bytes, rx_buffer to 16MB (protocol: {})",
        batch_size, protocol
    );

    // Increase queue sizes to handle large payload bursts (default is 2, max is 16)
    // This allows more batches to be queued before back-pressure kicks in
    config
        .transport
        .link
        .tx
        .queue
        .size
        .set_data(16)
        .map_err(|e| format!("config error: {e:?}"))?;
    config
        .transport
        .link
        .tx
        .queue
        .size
        .set_data_high(16)
        .map_err(|e| format!("config error: {e:?}"))?;
    config
        .transport
        .link
        .tx
        .queue
        .size
        .set_data_low(16)
        .map_err(|e| format!("config error: {e:?}"))?;

    // Send immediately without waiting to batch
    config
        .transport
        .link
        .tx
        .queue
        .batching
        .set_enabled(false)
        .map_err(|e| format!("config error: {e:?}"))?;

    // Increase wait_before_close timeout for Block congestion control (default: 5 seconds)
    // Set to 5 minutes (300 seconds = 300_000_000 microseconds) to allow large transfers
    config
        .transport
        .link
        .tx
        .queue
        .congestion_control
        .block
        .set_wait_before_close(300_000_000)
        .map_err(|e| format!("config error: {e:?}"))?;
    info!("Set queue sizes to 16, batching disabled, wait_before_close to 300 seconds");

    // Parse and apply any additional configuration provided as JSON
    if !config_json.is_empty() && config_json != "{}" {
        debug!("Parsing additional config: {}", config_json);
        if let Ok(additional_config) = serde_json::from_str::<serde_json::Value>(config_json) {
            if let Ok(zenoh_config) = serde_json::from_value(additional_config) {
                config = zenoh_config;
                debug!("Successfully applied additional config");
            }
        }
    }

    // Configure the connection mode
    if mode == "peer" {
        info!("Setting peer mode");
        config
            .set_mode(Some(WhatAmI::Peer))
            .map_err(|e| format!("config error: {e:?}"))?;

        // Enable scouting to allow peers for discovery via multicast
        info!("Peer mode - configuring scouting");
        config
            .scouting
            .multicast
            .set_enabled(Some(true))
            .map_err(|e| format!("config error: {e:?}"))?;
        config
            .scouting
            .gossip
            .set_enabled(Some(true))
            .map_err(|e| format!("config error: {e:?}"))?;

        // Set default multicast address
        config
            .scouting
            .multicast
            .set_address(Some(
                "224.0.0.224:7446"
                    .parse()
                    .map_err(|e| format!("invalid multicast address: {e}"))?,
            ))
            .map_err(|e| format!("config error: {e:?}"))?;

        // Enable local routing
        // Note: routing.peer.mode is private in zenoh 1.0, skip this configuration

        // Add listening endpoints for peer mode
        // Each peer on the same machine should use a different listen port
        // Use [::] for IPv6 (default)
        // so_sndbuf/so_rcvbuf options only work for TCP/TLS, not UDP
        let port = parse_listen_port(listen_port)?;
        let listen_endpoint = format!("{}/[::]:{}", protocol, port);
        info!("Peer mode, listening on {}", listen_endpoint);
        let ep = listen_endpoint
            .parse()
            .map_err(|e| format!("invalid listen endpoint {listen_endpoint}: {e}"))?;
        config
            .listen
            .endpoints
            .set(vec![ep])
            .map_err(|e| format!("config error: {e:?}"))?;
    } else {
        info!("Setting client mode");
        config
            .set_mode(Some(WhatAmI::Client))
            .map_err(|e| format!("config error: {e:?}"))?;
        config
            .scouting
            .multicast
            .set_enabled(Some(false))
            .map_err(|e| format!("config error: {e:?}"))?;
    }

    // Parse the locator strings into endpoints
    // Supports multiple endpoints separated by commas
    if !locators.is_empty() {
        debug!("Parsing locators: {}", locators);

        // Parse endpoints - socket buffer options (so_sndbuf/so_rcvbuf) only work for TCP/TLS
        let endpoints: Vec<_> = locators
            .split(',')
            .map(|s| s.trim().to_string())
            .map(|s| s.parse())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| user_error(mode, locators, &format!("{e}")))?;

        // Apply the endpoints to the configuration
        config
            .connect
            .endpoints
            .set(endpoints.clone())
            .map_err(|e| format!("config error: {e:?}"))?;
        info!("Set {} endpoints", endpoints.len());

        // An explicit peer endpoint must be reached: without these, a peer retries
        // in the background and `open` succeeds for an unreachable target.
        if mode == "peer" {
            config
                .insert_json5("connect/timeout_ms", r#"{ "peer": 10000 }"#)
                .map_err(|e| format!("config error: {e:?}"))?;
            config
                .insert_json5("connect/exit_on_failure", r#"{ "peer": true }"#)
                .map_err(|e| format!("config error: {e:?}"))?;
        }
    } else {
        info!("No locators, using only multicast discovery");
    }

    // Open the Zenoh session with the configured settings
    info!("Opening Zenoh session with mode: {:?}", mode);
    info!(
        "Final config - connect endpoints: {:?}",
        config.connect.endpoints
    );
    if mode == "peer" {
        info!(
            "Peer mode - listen endpoints: {:?}",
            config.listen.endpoints
        );
        info!(
            "Peer mode - multicast enabled: {:?}",
            config.scouting.multicast.enabled()
        );
    }

    // Use tokio timeout to prevent indefinite hanging
    let open_future = zenoh::open(config);
    info!("Starting Zenoh session open...");

    match tokio::time::timeout(std::time::Duration::from_secs(30), open_future).await {
        Ok(Ok(session)) => {
            info!("Successfully connected to Zenoh network in {} mode", mode);

            // In peer mode, let's give the session a moment to fully establish
            if mode == "peer" {
                info!("Peer mode: waiting for session to stabilize...");
                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            }

            Ok(session)
        }
        Ok(Err(e)) => {
            error!("Failed to connect in {} mode: {}", mode, e);
            Err(user_error(mode, locators, &e.to_string()).into())
        }
        Err(_) => {
            error!("Connection timeout after 30 seconds in {} mode", mode);
            Err(format!(
                "Connection timeout in {} mode: Unable to establish connection within 30 seconds",
                mode
            )
            .into())
        }
    }
}

/// Connect a monitor session for observing all network traffic.
///
/// The monitor always opens in client mode, listens on nothing and has multicast
/// and gossip scouting off. It dials the publishing session's own listener (peer
/// mode) or the same routers (client mode), because a peer or router forwards
/// samples to its clients: so the monitor sees third-party traffic and is not
/// counted as a peer.
///
/// # Arguments
/// * `locators` - The publishing session's connection endpoints
/// * `listen_port` - The publishing session's listen port
/// * `mode` - The publishing session's mode (peer or client)
pub async fn connect_zenoh_monitor(
    locators: &str,
    listen_port: &str,
    mode: &str,
) -> Result<Session, Box<dyn std::error::Error + Send + Sync>> {
    info!(
        "Attempting to connect monitor session - publishing mode: {}, locators: {}, listen_port: {}",
        mode,
        if locators.is_empty() {
            "(none - using discovery)"
        } else {
            locators
        },
        listen_port
    );

    let mut config = zenoh::config::Config::default();

    let protocol = listen_protocol(locators);
    let is_udp_based = protocol == "udp" || protocol == "quic";

    // Set batch size based on protocol
    let batch_size: u16 = if is_udp_based { 1472 } else { 65535 };
    config
        .transport
        .link
        .tx
        .set_batch_size(batch_size)
        .map_err(|e| format!("config error: {e:?}"))?;
    config
        .transport
        .link
        .rx
        .set_buffer_size(16 * 1024 * 1024)
        .map_err(|e| format!("config error: {e:?}"))?;

    // Client mode, no listener, no scouting. zenoh's client defaults
    // (connect timeout 0, exit on failure) make `open` fail at once when
    // nothing answers.
    config
        .set_mode(Some(WhatAmI::Client))
        .map_err(|e| format!("config error: {e:?}"))?;
    config
        .listen
        .endpoints
        .set(vec![])
        .map_err(|e| format!("config error: {e:?}"))?;
    config
        .scouting
        .multicast
        .set_enabled(Some(false))
        .map_err(|e| format!("config error: {e:?}"))?;
    config
        .scouting
        .gossip
        .set_enabled(Some(false))
        .map_err(|e| format!("config error: {e:?}"))?;

    let targets = monitor_endpoints(locators, listen_port, mode)?;
    let endpoints = targets
        .iter()
        .map(|t| {
            t.parse()
                .map_err(|e| format!("invalid monitor endpoint {t}: {e}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    config
        .connect
        .endpoints
        .set(endpoints)
        .map_err(|e| format!("config error: {e:?}"))?;
    info!("Monitor session: client mode, dialling {:?}", targets);

    // Open the monitor session with a shorter timeout
    info!("Opening monitor Zenoh session...");

    match tokio::time::timeout(std::time::Duration::from_secs(15), zenoh::open(config)).await {
        Ok(Ok(session)) => {
            info!("Successfully connected monitor session in client mode");
            // Brief stabilization delay
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
            Ok(session)
        }
        Ok(Err(e)) => {
            error!("Monitor session failed to connect: {}", e);
            Err(e.to_string().into())
        }
        Err(_) => {
            error!("Monitor session connection timeout");
            Err("connection timeout after 15 seconds".into())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn user_error_drops_bang_before_space() {
        // The old cut stopped at the first " at " and trimmed '!' before the spaces.
        assert_eq!(
            user_error("peer", "", "retry at least once! \n at /x/y.rs:3."),
            "Could not connect in peer mode: retry at least once"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn endpoint_parse_error_is_a_user_error() {
        // Fails while parsing the locator, before any session opens.
        let e = connect_zenoh("not-a-locator", "7447", "client", "{}")
            .await
            .expect_err("an unparsable locator must fail")
            .to_string();
        assert!(e.starts_with("Could not connect in client mode: "), "{e}");
        assert!(!e.contains(".rs:"), "{e}");
    }

    #[test]
    fn listen_port_rejects_zero_and_garbage() {
        assert_eq!(parse_listen_port(" 7447 "), Ok(7447));
        assert_eq!(parse_listen_port("65000"), Ok(65000)); // no second port at + 1000 any more
        for bad in ["", "abc", "0", "70000"] {
            assert!(parse_listen_port(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn connect_error_text_has_no_source_path() {
        let raw = "Unable to connect to any of [tcp/localhost:7447]! at /home/u/.cargo/registry/src/x/zenoh-1.10.1/src/net/runtime/orchestrator.rs:374.";
        let t = user_error("client", "tcp/localhost:7447", raw);
        assert!(!t.contains(".rs:") && !t.contains("/home/"), "{t}");
        assert!(user_error("client", "", "No peer specified").contains("needs a router address"));
    }

    #[test]
    fn monitor_endpoints_follow_publishing_mode() {
        assert_eq!(
            monitor_endpoints("", "7447", "peer").unwrap(),
            ["tcp/[::1]:7447", "tcp/127.0.0.1:7447"]
        );
        // Peer mode with an address still dials this app's own listener, on the listener's transport.
        assert_eq!(
            monitor_endpoints("udp/10.0.0.5:7447", "7450", "peer").unwrap(),
            ["udp/[::1]:7450", "udp/127.0.0.1:7450"]
        );
        assert_eq!(
            monitor_endpoints("tcp/r1:7447, tcp/r2:7447", "", "client").unwrap(),
            ["tcp/r1:7447", "tcp/r2:7447"]
        );
        assert!(monitor_endpoints("", "7447", "client").is_err());
        assert!(monitor_endpoints("", "0", "peer").is_err());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "opens network sessions"]
    async fn peer_mode_unreachable_endpoint_fails() {
        use std::time::Duration;
        let opened = tokio::time::timeout(
            Duration::from_secs(15),
            connect_zenoh("tcp/10.255.255.1:7447", "27701", "peer", "{}"),
        )
        .await
        .expect("connect_zenoh did not return within 15 s");
        if let Ok(session) = opened {
            let _ = session.close().await;
            panic!("an unreachable peer-mode endpoint must fail the connect");
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "opens network sessions"]
    async fn monitor_sees_third_party_samples() {
        use std::time::Duration;
        // As the form opens them: peer mode, empty address (multicast on), Listen Port 27802.
        let s = connect_zenoh("", "27802", "peer", "{}")
            .await
            .expect("publishing session");
        let m = connect_zenoh_monitor("", "27802", "peer")
            .await
            .expect("monitor session");
        let sub = m
            .declare_subscriber("**")
            .await
            .expect("monitor subscriber");
        // A third peer that only dials this app's listener, as another app would.
        let mut c = zenoh::Config::default();
        for (k, v) in [
            ("mode", r#""peer""#),
            ("listen/endpoints", "[]"),
            ("scouting/multicast/enabled", "false"),
            ("connect/endpoints", r#"["tcp/[::1]:27802"]"#),
        ] {
            c.insert_json5(k, v).expect(k);
        }
        let third = zenoh::open(c).await.expect("third session");
        // Declarations propagate asynchronously, so put again until the monitor sees one.
        let received = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                third.put("t/m", "x").await.expect("put");
                let next = tokio::time::timeout(Duration::from_millis(250), sub.recv_async()).await;
                if let Ok(Ok(sample)) = next {
                    if sample.key_expr().as_str() == "t/m" {
                        break;
                    }
                }
            }
        })
        .await
        .is_ok();
        let monitor_is_peer = s.info().peers_zid().await.any(|z| z == m.zid());
        let _ = third.close().await;
        let _ = m.close().await;
        let _ = s.close().await;
        assert!(
            received,
            "the monitor's ** subscriber did not get the third peer's t/m within 5 s"
        );
        assert!(
            !monitor_is_peer,
            "the monitor must not count as a peer (T22's peer count)"
        );
    }
}

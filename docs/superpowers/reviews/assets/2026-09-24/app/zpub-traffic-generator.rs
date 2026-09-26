//! Review-evidence traffic generator: peer-mode session publishing demo topics
//! plus a partial chunked transfer (3 of 5 chunks) so the explorer shows an
//! active transfer. Chunk payloads are tiny; only the key metadata matters.
use std::time::Duration;
#[tokio::main]
async fn main() {
    let s = zenoh::open(zenoh::Config::default()).await.unwrap();
    let chunk = 64 * 1024 * 1024usize;
    let total = 4 * chunk + 1000; // 5 chunks
    let mut tick = 0u64;
    loop {
        s.put("demo/sensors/temp1", format!("{:.1}", 21.0 + (tick % 10) as f64 / 10.0)).await.unwrap();
        s.put("demo/sensors/humidity", format!("{}", 40 + tick % 5)).await.unwrap();
        s.put("demo/robot/status", format!(r#"{{"state":"running","battery":{},"pose":{{"x":1.2,"y":3.4}}}}"#, 90 - tick % 10))
            .encoding(zenoh::bytes::Encoding::APPLICATION_JSON).await.unwrap();
        s.put("demo/logs/app", format!("tick {tick}: all systems nominal")).await.unwrap();
        s.put("demo/bin/blob", vec![0u8, 1, 2, 250, 251, 252]).await.unwrap();
        if tick < 3 {
            s.put(format!("demo/files/report/__chunk/{total}/5/{tick}"), vec![7u8; 16])
                .attachment("report.pdf").await.unwrap();
        }
        tick += 1;
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}

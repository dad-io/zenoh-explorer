//! Help tab rendering.

use egui::RichText;

use crate::app::ZenohExplorer;
use crate::types::*;

/// Every Help heading and its lines, in screen order. Kept as data so tests can
/// check the text against the app's behaviour.
pub(crate) const HELP_SECTIONS: &[(&str, &[&str])] = &[
    (
        "What it is",
        &["Watch, publish and query data on a Zenoh network."],
    ),
    (
        "Getting started",
        &[
            "1. Connect: Peer (the default) finds other peers on the local network by multicast (UDP 7446). Listen Port is where other peers reach this app. Use a different Listen Port for each copy on one machine. Address is optional.",
            "   Client connects to a router: enter its address (for example localhost) and port (7447).",
            "   Tested with tcp and multicast; other transports are offered but untested.",
            "2. Once connected, a background ** monitor adds every key this app receives to the tree. Subscribe to Topics, above the tree, adds a subscription of your own, such as demo/**; you need one when the header says \"monitor off\".",
            "3. The topic tree on the left fills as messages arrive. Select a topic for its value and history; select a branch for a summary of what is below it.",
            "4. All Messages (Topics view with no topic selected) lists recent messages this app received or published, including query replies, and holds the Memory, Message and Rate Limit fields. New messages on paused topics (except query replies) and file chunks are not listed.",
            "5. Publish: send text, or import a file (it is read into memory).",
            "6. Query: ask queryables for values. Results show each reply; a query with no match ends at once.",
            "7. Queryable (Publish tab): answers queries with the last value this app published on each key (typed text only, up to 10 MB; not imports).",
        ],
    ),
    (
        "Key expressions",
        &[
            "** : every key except @ admin keys",
            "demo/** : every key under demo/",
            "sensor/*/temperature : one level in the middle",
            "device/1/status : exactly this key",
            "Keys have no empty levels (no //, and no / at the start or end); * and ** fill a whole level.",
        ],
    ),
    (
        "Limits",
        &[
            "History keeps rows up to the Memory Limit (default 100 MB) and the Message Limit; older rows leave the list but stay in the tree and its counts.",
            "Messages over the Rate Limit are not listed, but the tree and Save still see them.",
            "Duplicates: the same sample seen by two sessions, or by two overlapping subscriptions, within 250 ms is listed once.",
            "Lists show the start of each value (about 200 bytes; Query Results 500); the topic's Current Value shows up to 10 KB; Save File writes all of it.",
        ],
    ),
    (
        "Troubleshooting",
        &[
            "Connected but the tree stays empty: check the peer count in the header (\"no peers\" means no Zenoh peer or router is linked to this app; apps in client mode that dial this app are not counted). If the header says \"monitor off\", subscribe (step 2).",
            "Connection error: the red message in the connection panel, above the Connect button, names the cause (the header shows its first words); in Client mode an address is required.",
            "A button is disabled: invalid input is named under its field. Otherwise the app is not connected (Publish and Query say so at the top; Subscribe needs a connection too), or that key is already subscribed. Save File's hover says why it is unavailable.",
            "A query says \"No replies\": no queryable matched, or those that matched had nothing to return. A timeout with no replies is shown as an error.",
            "After reconnecting, your subscriptions are re-declared automatically.",
        ],
    ),
];

/// Trait for help tab rendering.
pub trait HelpUI {
    fn show_help_tab(&mut self, ui: &mut egui::Ui);
}

impl HelpUI for ZenohExplorer {
    /// Renders the Help tab from `HELP_SECTIONS`, scrollable so the last line
    /// is reachable in a small window.
    fn show_help_tab(&mut self, ui: &mut egui::Ui) {
        ui.label(
            RichText::new("Zenoh Explorer Help")
                .size(HEADING_MEDIUM_SIZE)
                .strong(),
        );
        ui.separator();
        egui::ScrollArea::vertical()
            .id_salt("help")
            .auto_shrink([false; 2])
            .show(ui, |ui| {
                for (i, (heading, lines)) in HELP_SECTIONS.iter().enumerate() {
                    if i > 0 {
                        ui.separator();
                    }
                    ui.label(RichText::new(*heading).strong());
                    for line in *lines {
                        ui.label(*line);
                    }
                }
            });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all_text() -> String {
        HELP_SECTIONS
            .iter()
            .flat_map(|(h, ls)| std::iter::once(*h).chain(ls.iter().copied()))
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn help_names_only_real_places() {
        let t = all_text();
        for gone in ["Subscribe tab", "Browse tab", "Messages tab"] {
            assert!(
                !t.contains(gone),
                "Help names a place that does not exist: {gone}"
            );
        }
        assert!(t.contains("Subscribe to Topics"));
        assert!(t.contains(". Query: "), "Help has a Query step"); // G4-4: "Query" alone also matches "Queryable"
        assert!(t.contains("Troubleshooting"));
    }

    #[test]
    fn help_claims_match_limits() {
        let t = all_text();
        // The last two are false after T10: no second port, and the ** monitor fills the tree unsubscribed.
        for false_claim in [
            "any size",
            "greater than 10MB",
            "items in keyspace",
            "Match all keys",
            "dropped when limits",
            "Listen Port + 1000",
            "until you do",
        ] {
            assert!(!t.contains(false_claim), "{false_claim}");
        }
    }
}

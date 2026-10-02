//! Help tab rendering.

use egui::RichText;

use crate::app::ZenohExplorer;
use crate::types::*;

/// Help headings, the targets of `help_link`.
pub(crate) mod section {
    pub const WHAT_IT_IS: &str = "What it is";
    pub const GETTING_STARTED: &str = "Getting started";
    pub const KEY_EXPRESSIONS: &str = "Key expressions";
    pub const LIMITS: &str = "Limits";
    pub const TROUBLESHOOTING: &str = "Troubleshooting";
}

impl ZenohExplorer {
    /// A small "More in Help" link that opens the Help view at `section`. A
    /// frameless button, so its hit area is `MIN_TARGET` tall.
    pub(crate) fn help_link(&mut self, ui: &mut egui::Ui, section: &'static str) -> egui::Response {
        let text = RichText::new("More in Help")
            .size(TEXT_SMALL_SIZE)
            .underline()
            .color(ui.visuals().hyperlink_color);
        let response = ui
            .add(
                egui::Button::new(text)
                    .frame(false)
                    .min_size(egui::vec2(0.0, crate::app::theme::MIN_TARGET)),
            )
            .on_hover_cursor(egui::CursorIcon::PointingHand)
            .on_hover_text(format!("Help: {section}"));
        if response.clicked() {
            self.detail_view = DetailView::Help;
            self.help_target = Some(section);
            // A link in a view drawn after Help this frame still lands next frame.
            ui.ctx().request_repaint();
        }
        response
    }
}

/// Every Help heading and its lines, in screen order. Kept as data so tests can
/// check the text against the app's behaviour.
pub(crate) const HELP_SECTIONS: &[(&str, &[&str])] = &[
    (
        section::WHAT_IT_IS,
        &["Watch, publish and query data on a Zenoh network."],
    ),
    (
        section::GETTING_STARTED,
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
        section::KEY_EXPRESSIONS,
        &[
            "** : every key except @ admin keys",
            "demo/** : every key under demo/",
            "sensor/*/temperature : one level in the middle",
            "device/1/status : exactly this key",
            "Keys have no empty levels (no //, and no / at the start or end); * and ** fill a whole level.",
        ],
    ),
    (
        section::LIMITS,
        &[
            "History keeps rows up to the Memory Limit (default 100 MB) and the Message Limit; older rows leave the list but stay in the tree and its counts.",
            "Messages over the Rate Limit are not listed, but the tree and Save still see them.",
            "Duplicates: the same sample seen by two sessions, or by two overlapping subscriptions, within 250 ms is listed once.",
            "Lists show the start of each value (about 200 bytes; Query Results 500); the topic's Current Value shows up to 10 KB; Save File writes all of it.",
        ],
    ),
    (
        section::TROUBLESHOOTING,
        &[
            "Connected but the tree stays empty: check the peer count in the header (\"no peers\" means no Zenoh peer or router is linked to this app; apps in client mode that dial this app are not counted). If the header says \"monitor off\", subscribe (step 2).",
            "Connection error: the red message in the connection panel, above the Connect button, names the cause (the header shows its first words); in Client mode an address is required.",
            "A button is disabled: the reason is written beside it; invalid input is also named under its field.",
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
            // A Help link jumps, it does not glide: egui 0.29.1 applies an
            // animated scroll target a frame late (Snow White 011cc7d).
            .animated(false)
            .show(ui, |ui| {
                for (i, (heading, lines)) in HELP_SECTIONS.iter().enumerate() {
                    if i > 0 {
                        ui.separator();
                    }
                    let r = ui.label(RichText::new(*heading).strong());
                    if self.help_target == Some(*heading) {
                        r.scroll_to_me(Some(egui::Align::TOP));
                    }
                    for line in *lines {
                        ui.label(*line);
                    }
                }
            });
        // Scrolled once; a target that names no heading is dropped too.
        self.help_target = None;
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

    #[test]
    fn section_names_are_the_headings() {
        let headings: Vec<&str> = HELP_SECTIONS.iter().map(|(h, _)| *h).collect();
        assert_eq!(
            headings,
            [
                section::WHAT_IT_IS,
                section::GETTING_STARTED,
                section::KEY_EXPRESSIONS,
                section::LIMITS,
                section::TROUBLESHOOTING,
            ]
        );
    }

    #[test]
    fn troubleshooting_is_last() {
        // regression guard: P1's Help already ends with Troubleshooting
        assert_eq!(HELP_SECTIONS.last().unwrap().0, section::TROUBLESHOOTING);
    }

    #[test]
    fn troubleshooting_says_reasons_are_visible() {
        let t = all_text();
        assert!(
            !t.contains("hover says why"),
            "reasons are no longer hover-only"
        );
        assert!(t.contains("the reason is written beside it"));
    }

    fn help_panel(a: &mut ZenohExplorer, ui: &mut egui::Ui) {
        a.show_help_tab(ui)
    }

    #[test]
    fn help_target_scrolls_into_view() {
        use crate::app::headless::{text, Headless};
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(egui::vec2(1000.0, 300.0));
        let out = h.panel(&mut app, vec![], help_panel);
        assert!(
            text(&out, section::TROUBLESHOOTING).is_none(),
            "precondition: Troubleshooting is below a 300 pt window"
        );
        app.help_target = Some(section::TROUBLESHOOTING);
        let _ = h.panel(&mut app, vec![], help_panel);
        let out = h.panel(&mut app, vec![], help_panel);
        let heading = text(&out, section::TROUBLESHOOTING).expect("heading in view");
        assert!(
            heading.rect.min.y >= 0.0 && heading.rect.max.y <= 300.0,
            "{:?}",
            heading.rect
        );
        assert_eq!(app.help_target, None, "scrolled once");
    }

    #[test]
    fn help_link_opens_its_section() {
        use crate::app::headless::{node, Headless, PanelFn, WIDE};
        use crate::app::theme::MIN_TARGET;
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let show: PanelFn = |a, ui| {
            a.help_link(ui, section::LIMITS);
        };
        let out = h.panel(&mut app, vec![], show);
        let link = node(&out, "More in Help").expect("link");
        assert!(link.rect.height() >= MIN_TARGET, "{:?}", link.rect);
        let _ = h.click_panel(&mut app, link.rect.center(), show);
        assert_eq!(app.detail_view, DetailView::Help);
        assert_eq!(app.help_target, Some(section::LIMITS));
    }
}

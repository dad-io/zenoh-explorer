//! Messages tab rendering.

use std::collections::VecDeque;

use egui::{Color32, RichText};

use crate::app::ZenohExplorer;
use crate::types::*;

/// Rows rendered at most, newest first.
const MAX_RENDERED_MESSAGES: usize = 500;

/// Bytes of each payload the filter searches.
const SEARCH_BYTES: usize = 4 * 1024;

/// ASCII case-insensitive substring test that allocates nothing.
/// `needle` must be non-empty.
fn contains_ascii_ci(hay: &str, needle: &str) -> bool {
    hay.as_bytes()
        .windows(needle.len())
        .any(|w| w.eq_ignore_ascii_case(needle.as_bytes()))
}

/// Returns the newest `max` rows matching `filter`, oldest first, and the
/// total number of matching rows. A row matches when its key or the first
/// 4 KiB of its payload contains the filter, ignoring case.
pub(crate) fn filtered_tail<'a>(
    messages: &'a VecDeque<ZenohMessage>,
    filter: &str,
    max: usize,
) -> (Vec<&'a ZenohMessage>, usize) {
    if filter.is_empty() {
        let start = messages.len().saturating_sub(max);
        return (messages.iter().skip(start).collect(), messages.len());
    }
    let lowered = filter.to_lowercase();
    let ascii = lowered.is_ascii();
    let matches = |message: &ZenohMessage| {
        let end = safe_truncate_index(&message.payload, SEARCH_BYTES);
        let payload = &message.payload[..end];
        if ascii {
            contains_ascii_ci(&message.key, &lowered) || contains_ascii_ci(payload, &lowered)
        } else {
            message.key.to_lowercase().contains(&lowered)
                || payload.to_lowercase().contains(&lowered)
        }
    };
    let mut shown = Vec::new();
    let mut total = 0;
    for message in messages.iter().rev() {
        if matches(message) {
            total += 1;
            if shown.len() < max {
                shown.push(message);
            }
        }
    }
    shown.reverse();
    (shown, total)
}

/// Words the line that lists paused topics: sorted, at most three keys named.
fn paused_note(keys: &[&str]) -> String {
    let mut sorted = keys.to_vec();
    sorted.sort_unstable();
    let n = sorted.len();
    if n == 1 {
        return format!(
            "New messages on 1 paused topic are not listed: {}",
            sorted[0]
        );
    }
    let mut note = format!(
        "New messages on {n} paused topics are not listed: {}",
        sorted
            .iter()
            .take(3)
            .copied()
            .collect::<Vec<_>>()
            .join(", ")
    );
    if n > 3 {
        note.push_str(&format!(", and {} more", n - 3));
    }
    note
}

/// Trait for messages tab rendering.
pub trait MessagesUI {
    fn show_messages_tab(&mut self, ui: &mut egui::Ui);
}

impl MessagesUI for ZenohExplorer {
    /// Renders the Messages tab UI.
    /// Shows all network activity with filtering and auto-scroll capabilities.
    fn show_messages_tab(&mut self, ui: &mut egui::Ui) {
        // Message controls toolbar
        ui.horizontal(|ui| {
            ui.label("Filter:");
            ui.text_edit_singleline(&mut self.message_filter);
            ui.checkbox(&mut self.auto_scroll, "Auto-scroll");
            if ui.button("Clear").clicked() {
                self.messages.clear();
                self.current_memory_bytes = 0;
                self.messages_dropped = 0;
                self.rate_limit_drops = 0;
                self.messages_deduped = 0;
            }

            ui.separator();
            ui.label(format!(
                "In list: {} (limit {})",
                self.messages.len(),
                self.max_messages
            ));
        });

        // Memory management controls; wraps so Dedup and its Help link stay on screen
        ui.horizontal_wrapped(|ui| {
            ui.label("Memory Limit (MB):");
            let mut limit_str = self.max_memory_mb.to_string();
            if ui.text_edit_singleline(&mut limit_str).changed() {
                if let Ok(new_limit) = limit_str.parse::<usize>() {
                    self.max_memory_mb = new_limit.clamp(10, 1000); // Clamp between 10MB and 1GB
                }
            }

            ui.label("Message Limit:");
            let mut count_str = self.max_messages.to_string();
            if ui.text_edit_singleline(&mut count_str).changed() {
                if let Ok(new_limit) = count_str.parse::<usize>() {
                    self.max_messages = new_limit.clamp(100, 50000); // Clamp between 100 and 50k
                }
            }

            ui.label("Rate Limit (msg/s):");
            let mut rate_str = self.rate_limiter.max_messages_per_second.to_string();
            if ui.text_edit_singleline(&mut rate_str).changed() {
                if let Ok(new_rate) = rate_str.parse::<usize>() {
                    self.rate_limiter.max_messages_per_second = new_rate.clamp(10, 10000);
                    // 10-10k msg/s
                }
            }

            ui.checkbox(&mut self.deduper.enabled, "Dedup");
            if self.messages_deduped > 0 {
                ui.label(
                    RichText::new(format!("({} deduped)", self.messages_deduped))
                        .color(self.text_secondary_color())
                        .size(TEXT_SMALL_SIZE),
                );
            }
            self.help_link(ui, crate::ui::help::section::LIMITS);
        });

        if !self.paused_keys.is_empty() {
            let keys: Vec<&str> = self.paused_keys.iter().map(String::as_str).collect();
            let note = paused_note(&keys);
            let mut resume = false;
            ui.horizontal(|ui| {
                ui.label(
                    RichText::new(note)
                        .color(self.text_secondary_color())
                        .size(TEXT_SMALL_SIZE),
                );
                resume = ui.button("Resume all").clicked();
            });
            if resume {
                self.paused_keys.clear();
            }
        }

        let (shown, total) =
            filtered_tail(&self.messages, &self.message_filter, MAX_RENDERED_MESSAGES);
        if total > shown.len() {
            let text = if self.message_filter.is_empty() {
                format!("Showing the newest {} of {} rows", shown.len(), total)
            } else {
                format!(
                    "Showing the newest {} of {} matching rows",
                    shown.len(),
                    total
                )
            };
            ui.label(
                RichText::new(text)
                    .color(self.text_secondary_color())
                    .size(TEXT_SMALL_SIZE),
            );
        }

        let now = chrono::Utc::now();
        egui::ScrollArea::vertical()
            .id_salt("all_messages")
            .auto_shrink([false; 2])
            .stick_to_bottom(self.auto_scroll)
            .show(ui, |ui| {
                for message in shown {
                    ui.horizontal(|ui| {
                        // Message type badge
                        ui.label(
                            RichText::new(message.message_type.label())
                                .background_color(message.message_type.color())
                                .color(Color32::WHITE)
                                .size(TEXT_SMALL_SIZE),
                        );

                        // Timestamp, local time
                        ui.label(
                            RichText::new(format_local_time(&message.timestamp, &now))
                                .color(self.text_secondary_color())
                                .size(TEXT_SMALL_SIZE),
                        );

                        // Key
                        ui.label(RichText::new(&message.key).strong());
                    });

                    // Payload (truncated)
                    if !message.payload.is_empty() {
                        let display_payload = if message.payload.len() > 200 {
                            let end = safe_truncate_index(&message.payload, 200);
                            format!("{}...", &message.payload[..end])
                        } else {
                            message.payload.clone()
                        };
                        ui.label(
                            RichText::new(display_payload)
                                .color(self.text_secondary_color())
                                .size(TEXT_SMALL_SIZE),
                        );
                    }

                    ui.separator();
                }
            });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    fn m(key: &str) -> ZenohMessage {
        ZenohMessage::new_with_bytes(
            key.into(),
            "p".into(),
            vec![],
            "text/plain".into(),
            chrono::Utc::now(),
            MessageType::Subscribe,
            false,
            MessageSource::MonitorSession,
        )
    }

    #[test]
    fn filter_searches_whole_list_case_insensitively() {
        let mut list: VecDeque<ZenohMessage> = VecDeque::new();
        list.push_back(m("Old/Match"));
        for i in 0..600 {
            list.push_back(m(&format!("noise/{i}")));
        }
        let (shown, total) = filtered_tail(&list, "old/match", 500);
        assert_eq!(
            (shown.len(), total),
            (1, 1),
            "a match older than the newest 500 rows is still found"
        );
        let (shown, total) = filtered_tail(&list, "", 500);
        assert_eq!((shown.len(), total), (500, 601));
    }

    #[test]
    fn paused_note_is_singular_at_one_and_bounded() {
        assert_eq!(
            paused_note(&["a/x"]),
            "New messages on 1 paused topic are not listed: a/x"
        );
        assert_eq!(
            paused_note(&["e", "c", "a", "d", "b"]),
            "New messages on 5 paused topics are not listed: a, b, c, and 2 more"
        );
    }

    #[test]
    fn limits_link_and_controls_are_24() {
        use crate::app::headless::{node, Headless, WIDE};
        use crate::app::theme::MIN_TARGET;
        fn messages_panel(a: &mut ZenohExplorer, ui: &mut egui::Ui) {
            a.show_messages_tab(ui)
        }
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let out = h.panel(&mut app, vec![], messages_panel);
        for name in ["Clear", "Auto-scroll", "Dedup"] {
            let n = node(&out, name).unwrap_or_else(|| panic!("{name}"));
            assert!(n.rect.height() >= MIN_TARGET, "{name} {:?}", n.rect);
        }
        let link = node(&out, "More in Help").expect("the Limits row links Limits");
        let _ = h.click_panel(&mut app, link.rect.center(), messages_panel);
        assert_eq!(app.help_target, Some(crate::ui::help::section::LIMITS));
    }

    #[test]
    fn limits_row_stays_inside_the_whole_window() {
        use crate::app::headless::{node, Headless, WIDE};
        let sizes = [WIDE, egui::vec2(1000.0, 600.0)];
        for deduped in [0, 12_345] {
            for size in sizes {
                let (mut app, _tx) = ZenohExplorer::test_app();
                app.messages_deduped = deduped;
                let h = Headless::new(size);
                let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, size);
                let _ = h.run(vec![], |ctx| app.frame_ui(ctx));
                let out = h.run(vec![], |ctx| app.frame_ui(ctx));
                for name in ["Dedup", "More in Help"] {
                    let n = node(&out, name)
                        .unwrap_or_else(|| panic!("{name} at {size:?}, deduped {deduped}"));
                    assert!(
                        screen.contains_rect(n.rect),
                        "{name} {:?} lies outside the {size:?} window (deduped {deduped})",
                        n.rect
                    );
                }
            }
        }
    }
}

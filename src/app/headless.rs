//! Headless egui frames for tests: a fixed screen, the texts painted with
//! their rects, and AccessKit nodes with their bounds (no window, no GPU).
//! Replaces nothing: P1's `details_texts` helper keeps working as it is.

use egui::epaint::TextShape;
use egui::{
    pos2, Context, Event, FullOutput, Modifiers, PointerButton, Pos2, RawInput, Rect, Shape, Vec2,
};
use std::time::Duration;

use crate::app::ZenohExplorer;

/// A 1400 × 900 pt window.
pub(crate) const WIDE: Vec2 = egui::vec2(1400.0, 900.0);

/// A panel body under test, such as `|a, ui| a.show_tree_panel(ui)`.
pub(crate) type PanelFn = fn(&mut ZenohExplorer, &mut egui::Ui);

/// A text the frame painted, with its rect in points.
#[derive(Debug, Clone)]
pub(crate) struct Painted {
    pub text: String,
    pub rect: Rect,
}

/// An AccessKit node with a name and bounds (points; pixels_per_point is 1).
#[derive(Debug, Clone)]
pub(crate) struct Node {
    pub name: String,
    pub rect: Rect,
}

/// One egui context driven frame by frame at a fixed window size.
pub(crate) struct Headless {
    pub ctx: Context,
    size: Vec2,
}

impl Headless {
    pub fn new(size: Vec2) -> Self {
        let ctx = Context::default();
        ctx.enable_accesskit();
        Self { ctx, size }
    }

    /// One frame of `f` in the window.
    pub fn run(&self, events: Vec<Event>, f: impl FnMut(&Context)) -> FullOutput {
        let input = RawInput {
            screen_rect: Some(Rect::from_min_size(Pos2::ZERO, self.size)),
            events,
            ..Default::default()
        };
        self.ctx.run(input, f)
    }

    /// One frame of `f` inside a CentralPanel, with the app's theme applied.
    pub fn panel(&self, app: &mut ZenohExplorer, events: Vec<Event>, f: PanelFn) -> FullOutput {
        self.run(events, |ctx| {
            app.apply_theme(ctx);
            egui::CentralPanel::default().show(ctx, |ui| f(app, ui));
        })
    }

    /// A primary click at `at` (press frame, release frame), then a settled frame.
    pub fn click_panel(&self, app: &mut ZenohExplorer, at: Pos2, f: PanelFn) -> FullOutput {
        let (press, release) = click_events(at);
        let _ = self.panel(app, press, f);
        let _ = self.panel(app, release, f);
        self.panel(app, Vec::new(), f)
    }
}

/// Press events and release events for a primary click at `at`.
pub(crate) fn click_events(at: Pos2) -> (Vec<Event>, Vec<Event>) {
    let press = vec![
        Event::PointerMoved(at),
        Event::PointerButton {
            pos: at,
            button: PointerButton::Primary,
            pressed: true,
            modifiers: Modifiers::NONE,
        },
    ];
    let release = vec![Event::PointerButton {
        pos: at,
        button: PointerButton::Primary,
        pressed: false,
        modifiers: Modifiers::NONE,
    }];
    (press, release)
}

fn walk<'a>(shape: &'a Shape, out: &mut Vec<&'a TextShape>) {
    match shape {
        Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
        Shape::Text(t) => out.push(t),
        _ => {}
    }
}

/// Every text shape the frame painted (galleys keep their layout sections).
pub(crate) fn text_shapes(out: &FullOutput) -> Vec<&TextShape> {
    let mut v = Vec::new();
    for clipped in &out.shapes {
        walk(&clipped.shape, &mut v);
    }
    v
}

pub(crate) fn texts(out: &FullOutput) -> Vec<Painted> {
    text_shapes(out)
        .into_iter()
        .map(|t| Painted {
            text: t.galley.text().to_string(),
            rect: Rect::from_min_size(t.pos, t.galley.size()),
        })
        .collect()
}

pub(crate) fn text(out: &FullOutput, exact: &str) -> Option<Painted> {
    texts(out).into_iter().find(|t| t.text == exact)
}

pub(crate) fn nodes(out: &FullOutput) -> Vec<Node> {
    let Some(update) = &out.platform_output.accesskit_update else {
        return Vec::new();
    };
    update
        .nodes
        .iter()
        .filter_map(|(_, n)| {
            let name = n.name()?.to_string();
            let b = n.bounds()?;
            Some(Node {
                name,
                rect: Rect::from_min_max(
                    pos2(b.x0 as f32, b.y0 as f32),
                    pos2(b.x1 as f32, b.y1 as f32),
                ),
            })
        })
        .collect()
}

pub(crate) fn node(out: &FullOutput, name: &str) -> Option<Node> {
    nodes(out).into_iter().find(|n| n.name == name)
}

/// How long egui was asked to wait before the next frame.
pub(crate) fn repaint_delay(out: &FullOutput) -> Duration {
    out.viewport_output[&egui::ViewportId::ROOT].repaint_delay
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::theme::MIN_TARGET;

    #[test]
    fn headless_reads_texts_nodes_clicks_and_repaints() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let show: PanelFn = |a, ui| {
            ui.label("Hello");
            if ui.button("Hi").clicked() {
                a.tree_filter.push('x');
            }
            ui.ctx().request_repaint_after(Duration::from_millis(250));
        };
        let out = h.panel(&mut app, Vec::new(), show);
        assert!(text(&out, "Hello").is_some(), "{:?}", texts(&out));
        assert!(!text_shapes(&out).is_empty());
        let hi = node(&out, "Hi").expect("the button's AccessKit node");
        assert!(
            hi.rect.height() >= MIN_TARGET,
            "apply_theme makes a button {MIN_TARGET} pt tall: {:?}",
            hi.rect
        );
        assert!(repaint_delay(&out) <= Duration::from_millis(250));
        let _ = h.click_panel(&mut app, hi.rect.center(), show);
        assert_eq!(app.tree_filter, "x", "the click reached the button");
        assert!(nodes(&out).len() >= 2);
    }
}

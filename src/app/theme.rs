//! Theme colours, style application and small animations.

use eframe::egui;
use egui::Color32;

use crate::app::ZenohExplorer;
use crate::colors::ExplorerColors;

impl ZenohExplorer {
    pub(crate) fn background_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_BACKGROUND
        } else {
            ExplorerColors::BACKGROUND
        }
    }

    /// Returns the appropriate button style for the current theme
    pub(crate) fn apply_theme(&self, ctx: &egui::Context) {
        ctx.style_mut(|style| {
            style.animation_time = 0.001;
            if self.dark_mode {
                style.visuals.widgets.inactive.weak_bg_fill = ExplorerColors::DARK_PRIMARY;
                style.visuals.widgets.hovered.weak_bg_fill = ExplorerColors::DARK_PRIMARY_HOVER;
                style.visuals.widgets.active.weak_bg_fill = ExplorerColors::DARK_PRIMARY_HOVER;

                style.visuals.window_fill = ExplorerColors::DARK_BACKGROUND;
                style.visuals.panel_fill = ExplorerColors::DARK_CARD_BACKGROUND;
                style.visuals.extreme_bg_color = ExplorerColors::DARK_SURFACE;
                style.visuals.faint_bg_color = ExplorerColors::DARK_SIDEBAR;

                style.visuals.widgets.inactive.bg_fill = ExplorerColors::DARK_SURFACE;
                style.visuals.widgets.hovered.bg_fill = Color32::from_gray(70);
                style.visuals.widgets.active.bg_fill = ExplorerColors::DARK_SURFACE;

                style.visuals.widgets.inactive.bg_stroke.color = Color32::from_gray(100);
                style.visuals.widgets.hovered.bg_stroke.color = ExplorerColors::DARK_PRIMARY;
                style.visuals.widgets.active.bg_stroke.color = ExplorerColors::DARK_PRIMARY;

                style.visuals.widgets.inactive.fg_stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;
                style.visuals.widgets.hovered.fg_stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;
                style.visuals.widgets.active.fg_stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;

                style.visuals.widgets.noninteractive.bg_fill = ExplorerColors::DARK_CARD_BACKGROUND;
                style.visuals.widgets.noninteractive.fg_stroke.color =
                    ExplorerColors::DARK_TEXT_PRIMARY;

                style.visuals.code_bg_color = Color32::from_gray(30);

                style.visuals.selection.bg_fill = ExplorerColors::DARK_SELECTED_BACKGROUND;
                style.visuals.selection.stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;

                style.visuals.override_text_color = Some(ExplorerColors::DARK_TEXT_PRIMARY);
            } else {
                style.visuals.widgets.inactive.weak_bg_fill = ExplorerColors::PRIMARY;
                style.visuals.widgets.hovered.weak_bg_fill = ExplorerColors::PRIMARY_HOVER;
                style.visuals.widgets.active.weak_bg_fill = ExplorerColors::PRIMARY_HOVER;

                style.visuals.window_fill = ExplorerColors::BACKGROUND;
                style.visuals.panel_fill = ExplorerColors::CARD_BACKGROUND;
                style.visuals.extreme_bg_color = ExplorerColors::SURFACE;
                style.visuals.faint_bg_color = ExplorerColors::SIDEBAR;

                style.visuals.widgets.inactive.bg_fill = Color32::WHITE;
                style.visuals.widgets.hovered.bg_fill = Color32::from_gray(250);
                style.visuals.widgets.active.bg_fill = Color32::WHITE;

                style.visuals.widgets.inactive.bg_stroke.color = Color32::from_gray(200);
                style.visuals.widgets.hovered.bg_stroke.color = ExplorerColors::PRIMARY;
                style.visuals.widgets.active.bg_stroke.color = ExplorerColors::PRIMARY;

                style.visuals.widgets.inactive.fg_stroke.color = ExplorerColors::TEXT_PRIMARY;
                style.visuals.widgets.hovered.fg_stroke.color = ExplorerColors::TEXT_PRIMARY;
                style.visuals.widgets.active.fg_stroke.color = ExplorerColors::TEXT_PRIMARY;

                style.visuals.widgets.noninteractive.bg_fill = ExplorerColors::CARD_BACKGROUND;
                style.visuals.widgets.noninteractive.fg_stroke.color = ExplorerColors::TEXT_PRIMARY;

                style.visuals.code_bg_color = Color32::from_gray(240);

                style.visuals.selection.bg_fill = ExplorerColors::SELECTED_BACKGROUND;
                style.visuals.selection.stroke.color = Color32::WHITE;

                style.visuals.override_text_color = Some(ExplorerColors::TEXT_PRIMARY);
            }
        });
    }

    #[allow(dead_code)]
    pub(crate) fn card_background_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_CARD_BACKGROUND
        } else {
            ExplorerColors::CARD_BACKGROUND
        }
    }

    pub(crate) fn text_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_TEXT_PRIMARY
        } else {
            ExplorerColors::TEXT_PRIMARY
        }
    }

    pub(crate) fn text_secondary_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_TEXT_SECONDARY
        } else {
            ExplorerColors::TEXT_SECONDARY
        }
    }

    pub(crate) fn text_tertiary_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_TEXT_TERTIARY
        } else {
            ExplorerColors::TEXT_TERTIARY
        }
    }

    /// Create smooth fade animation for UI elements
    pub(crate) fn animate_fade_in(&self, ctx: &egui::Context, id: &str, target: f32) -> f32 {
        ctx.animate_value_with_time(egui::Id::new(id), target, 0.001)
    }

    /// Create pulsing animation for warning indicators
    pub(crate) fn animate_pulse(&self, ctx: &egui::Context, _id: &str) -> f32 {
        let time = ctx.input(|i| i.time) as f32;
        0.85 + (time * 3.0).sin() * 0.15
    }
}

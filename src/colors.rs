use egui::Color32;

/// Color scheme for the Zenoh Explorer UI.
/// Provides both light and dark mode color palettes following modern design principles.
/// Colors are optimized for readability and visual hierarchy.
#[cfg_attr(test, derive(Debug))]
pub struct ExplorerColors;
#[allow(dead_code)] // Theme palette includes colors for future UI expansion
impl ExplorerColors {
    // Light mode colors
    pub const BACKGROUND: Color32 = Color32::from_rgb(248, 248, 248);
    pub const CARD_BACKGROUND: Color32 = Color32::from_rgb(255, 255, 255);
    pub const SIDEBAR: Color32 = Color32::from_rgb(242, 242, 247);
    pub const PRIMARY: Color32 = Color32::from_rgb(0, 122, 255);
    pub const PRIMARY_HOVER: Color32 = Color32::from_rgb(0, 102, 217);
    pub const SUCCESS: Color32 = Color32::from_rgb(52, 199, 89);
    pub const WARNING: Color32 = Color32::from_rgb(255, 149, 0);
    pub const ERROR: Color32 = Color32::from_rgb(255, 59, 48);
    pub const TEXT_PRIMARY: Color32 = Color32::from_rgb(28, 28, 30);      // Almost black - high contrast
    pub const TEXT_SECONDARY: Color32 = Color32::from_rgb(60, 60, 67);    // Dark gray - readable (was 99,99,102)
    pub const SEPARATOR: Color32 = Color32::from_rgba_premultiplied(0, 0, 0, 26);
    pub const SELECTED_BACKGROUND: Color32 = Color32::from_rgba_premultiplied(0, 122, 255, 25);
    pub const TEXT_TERTIARY: Color32 = Color32::from_rgb(99, 99, 102);    // Medium gray (swapped with secondary)
    pub const SURFACE: Color32 = Color32::from_rgb(250, 250, 250);

    // Dark mode colors
    pub const DARK_BACKGROUND: Color32 = Color32::from_rgb(45, 45, 45);
    pub const DARK_CARD_BACKGROUND: Color32 = Color32::from_rgb(75, 75, 75);
    pub const DARK_SIDEBAR: Color32 = Color32::from_rgb(55, 55, 55);
    pub const DARK_PRIMARY: Color32 = Color32::from_rgb(10, 132, 255);
    pub const DARK_PRIMARY_HOVER: Color32 = Color32::from_rgb(64, 156, 255);
    pub const DARK_SUCCESS: Color32 = Color32::from_rgb(48, 209, 88);
    pub const DARK_WARNING: Color32 = Color32::from_rgb(255, 159, 10);
    pub const DARK_ERROR: Color32 = Color32::from_rgb(255, 69, 58);
    pub const DARK_TEXT_PRIMARY: Color32 = Color32::from_rgb(255, 255, 255);
    pub const DARK_TEXT_SECONDARY: Color32 = Color32::from_rgb(200, 200, 200); // Increased from 180 for better contrast
    pub const DARK_SEPARATOR: Color32 = Color32::from_rgba_premultiplied(255, 255, 255, 30);
    pub const DARK_SELECTED_BACKGROUND: Color32 =
        Color32::from_rgba_premultiplied(10, 132, 255, 40);
    pub const DARK_TEXT_TERTIARY: Color32 = Color32::from_rgb(180, 180, 180); // Brighter for better readability
    pub const DARK_SURFACE: Color32 = Color32::from_rgb(60, 60, 60);
}

#!/usr/bin/env bash
# T4 control grep. Run from repo root: bash docs/superpowers/reviews/assets/2026-09-24/t4/grep-controls.sh
set -u
FILES=(src/app.rs src/ui/help.rs src/ui/messages.rs src/ui/mod.rs src/ui/publish.rs src/ui/query.rs src/ui/topic_tree.rs)
PATTERNS=(
  '\.button\('
  'Button::new'
  'small_button'
  'selectable_label'
  'selectable_value'
  'ComboBox'
  'checkbox\('
  'Checkbox'
  'text_edit_singleline'
  'text_edit_multiline'
  'TextEdit::'
  'DragValue'
  'Slider'
  'radio'
  'toggle_value'
  'hyperlink'
  'menu_button'
  'ui\.interact'
  'Sense::click'
  'Sense::'
  '\.collapsing\('
  'CollapsingHeader'
  'CollapsingState'
  'show_toggle_button'
  '\.resizable\('
  '\.clicked\(\)'
  '\.changed\(\)'
  'add_enabled'
  'on_hover_text'
)
echo "== per-pattern counts (grep -nE, files: ${FILES[*]}) =="
for p in "${PATTERNS[@]}"; do
  printf '%-22s %s\n' "$p" "$(grep -nE "$p" "${FILES[@]}" | wc -l | tr -d ' ')"
done
echo
echo "== union of control-constructor patterns (distinct lines) =="
grep -nE '\.button\(|Button::new|small_button|selectable_label|selectable_value|ComboBox|checkbox\(|text_edit_singleline|TextEdit::|\.collapsing\(|show_toggle_button|SidePanel::|Sense::' "${FILES[@]}"
echo
echo "== .clicked() / .changed() sites =="
grep -nE '\.clicked\(\)|\.changed\(\)' "${FILES[@]}"

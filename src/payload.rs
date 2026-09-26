//! Bounded, allocation-light payload previews for display.

/// Maximum bytes rendered as hex for a binary payload.
pub const HEX_PREVIEW_BYTES: usize = 256;

/// Build a display preview of at most `max_text` bytes of text (plus a size
/// suffix) without copying the whole payload.
pub fn preview(bytes: &[u8], max_text: usize) -> String {
    let head = &bytes[..bytes.len().min(max_text)];
    let text = match std::str::from_utf8(head) {
        Ok(s) => Some(s),
        // Incomplete trailing char (the cut split it): keep the valid prefix.
        Err(e) if e.error_len().is_none() => Some(
            std::str::from_utf8(&head[..e.valid_up_to()]).expect("valid_up_to prefix is UTF-8"),
        ),
        Err(_) => None,
    };
    match text {
        Some(s) if s.len() == bytes.len() => s.to_string(),
        Some(s) => format!("{}... [+{} bytes]", s, bytes.len() - s.len()),
        None => {
            let shown = bytes.len().min(HEX_PREVIEW_BYTES);
            let hex: Vec<String> = bytes[..shown]
                .iter()
                .map(|b| format!("{:02x}", b))
                .collect();
            let more = if bytes.len() > shown { "..." } else { "" };
            format!("[binary {} bytes] {}{}", bytes.len(), hex.join(" "), more)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preview_empty() {
        assert_eq!(preview(b"", 10), "");
    }

    #[test]
    fn preview_short_text_is_verbatim() {
        assert_eq!(preview(b"hello", 10), "hello");
    }

    #[test]
    fn preview_long_text_is_cut_with_suffix() {
        assert_eq!(preview(b"hello world", 5), "hello... [+6 bytes]");
    }

    #[test]
    fn preview_keeps_text_when_cut_mid_char() {
        let s = "é".repeat(10); // 20 bytes
        assert_eq!(preview(s.as_bytes(), 5), "éé... [+16 bytes]");
    }

    #[test]
    fn preview_binary_is_hex() {
        assert_eq!(preview(&[0xff, 0x00], 10), "[binary 2 bytes] ff 00");
    }

    #[test]
    fn preview_long_binary_is_capped() {
        let p = preview(&vec![0xffu8; 1000], 10_000);
        assert!(p.starts_with("[binary 1000 bytes] ff"));
        assert!(p.ends_with("..."));
        assert_eq!(p.matches("ff").count(), HEX_PREVIEW_BYTES);
    }
}

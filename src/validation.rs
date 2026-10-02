//! Pre-flight validation of user-typed key expressions, selectors, timeouts and ports.

/// None if `s` is a valid canonical key expression, else a short reason.
pub fn key_expr_error(s: &str) -> Option<String> {
    if s.trim().is_empty() {
        return Some("Key expression is empty".into());
    }
    if s != s.trim() {
        return Some("Key has a leading or trailing space".into());
    }
    let err = zenoh::key_expr::KeyExpr::try_from(s).err()?;
    // zenoh reports a lone `$*` chunk with its empty-chunk text; name the real rule.
    let chunks = || s.split('/');
    if chunks().any(|c| c == "$*") && !chunks().any(str::is_empty) {
        return Some("`$*` must be joined to other text in its level, e.g. `v$*`".into());
    }
    Some(strip_source_path(&err.to_string()))
}

/// Drops every zenoh ` at <path>.rs:<line>.` suffix; all other text stays.
pub(crate) fn strip_source_path(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(i) = rest.find(" at ") {
        let after = &rest[i + 4..];
        let end = after.find(char::is_whitespace).unwrap_or(after.len());
        if after[..end].contains(".rs:") {
            out.push_str(&rest[..i]);
        } else {
            out.push_str(&rest[..i + 4 + end]);
        }
        rest = &after[end..];
    }
    out.push_str(rest);
    out
}

/// None if `s` is a valid selector (`key_expr[?params]`), else a short reason.
pub fn selector_error(s: &str) -> Option<String> {
    let key = s.split_once('?').map_or(s, |(k, _)| k);
    key_expr_error(key).or_else(|| {
        zenoh::query::Selector::try_from(s)
            .err()
            .map(|e| strip_source_path(&e.to_string()))
    })
}

/// None if `s` is a whole number of milliseconds in 100..=600_000.
pub fn timeout_error(s: &str) -> Option<String> {
    match s.trim().parse::<u64>() {
        Ok(v) if (100..=600_000).contains(&v) => None,
        _ => Some("Timeout must be 100–600000 ms".into()),
    }
}

/// A neutral note for a valid publish key that contains a wildcard.
pub fn wildcard_note(s: &str) -> Option<&'static str> {
    (key_expr_error(s).is_none() && s.contains('*'))
        .then_some("Wildcard key: every matching subscriber receives this")
}

/// None when `s.trim()` is a port number inside `range`, else a short reason.
pub fn port_error(s: &str, range: std::ops::RangeInclusive<u16>) -> Option<String> {
    match s.trim().parse::<u16>() {
        Ok(p) if range.contains(&p) => None,
        _ => Some(format!("Port must be {}–{}", range.start(), range.end())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strip_source_path_removes_every_suffix() {
        assert_eq!(strip_source_path("k: boom at /x/y.rs:3."), "k: boom");
        assert_eq!(
            strip_source_path("k: boom at /x/y.rs:3. - Caused by inner at /a/b.rs:9."),
            "k: boom - Caused by inner"
        );
        assert_eq!(
            strip_source_path("retry at least once at /x/y.rs:3."),
            "retry at least once"
        );
    }

    #[test]
    fn rejects_invalid_key_exprs() {
        for bad in ["", "  ", "demo/", "/demo", "a//b", "a/**/**", "a#b", "a?b"] {
            assert!(key_expr_error(bad).is_some(), "accepted {bad:?}");
        }
    }

    #[test]
    fn accepts_valid_key_exprs() {
        for ok in ["demo/**", "demo/*/x", "a/b$*", "@/router/x"] {
            assert_eq!(key_expr_error(ok), None, "rejected {ok:?}");
        }
    }

    #[test]
    fn selectors_allow_parameters() {
        assert_eq!(selector_error("demo/*/x?y=1;_time=[now(-1h)..]"), None);
        assert!(selector_error("demo/?y=1").is_some());
    }

    #[test]
    fn timeout_bounds() {
        assert_eq!(timeout_error("10000"), None);
        assert!(timeout_error("5s").is_some());
        assert!(timeout_error("0").is_some());
        assert!(timeout_error("700000").is_some());
    }

    #[test]
    fn error_text_has_no_source_path() {
        let e = key_expr_error("demo//x").unwrap();
        assert!(!e.contains(".rs:"), "{e}");
        assert!(
            key_expr_error("$*").unwrap().contains("$*"),
            "the lone-$* rule is named"
        );
    }

    #[test]
    fn surrounding_space_is_rejected() {
        assert!(key_expr_error(" demo/**").is_some());
        assert!(selector_error("demo/** ").is_some());
    }

    #[test]
    fn port_bounds() {
        assert_eq!(port_error("7447", 1..=65535), None);
        for bad in ["", "abc", "99999", "0"] {
            assert!(port_error(bad, 1..=65535).is_some(), "{bad}");
        }
        assert!(port_error("80", 1024..=65535).is_some());
    }
}

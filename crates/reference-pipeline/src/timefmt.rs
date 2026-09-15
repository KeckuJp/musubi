#[must_use]
pub fn iso_utc_ms(unix_ms: i64) -> String {
    let (date, hms, ms) = split(unix_ms);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        date.0, date.1, date.2, hms.0, hms.1, hms.2, ms
    )
}

#[must_use]
pub fn hms_utc(unix_ms: i64) -> String {
    let (_, hms, _) = split(unix_ms);
    format!("{:02}:{:02}:{:02}", hms.0, hms.1, hms.2)
}

#[must_use]
pub fn date_utc(unix_ms: i64) -> String {
    let (d, _, _) = split(unix_ms);
    format!("{:04}-{:02}-{:02}", d.0, d.1, d.2)
}

#[must_use]
pub fn rel_s(ms: i64) -> String {
    let sign = if ms < 0 { "-" } else { "+" };
    let a = ms.abs();
    format!("{sign}{}.{:01}s", a / 1000, (a % 1000) / 100)
}

#[must_use]
pub fn pm_s(bound_ms: i64) -> String {
    if bound_ms <= 0 {
        return "±0s".to_string();
    }
    if bound_ms < 1000 {
        return format!("±{bound_ms}ms");
    }
    format!("±{}.{:01}s", bound_ms / 1000, (bound_ms % 1000) / 100)
}

fn split(unix_ms: i64) -> ((i64, u32, u32), (u32, u32, u32), u32) {
    let secs = unix_ms.div_euclid(1000);
    let ms = unix_ms.rem_euclid(1000) as u32;
    let days = secs.div_euclid(86_400);
    let sod = secs.rem_euclid(86_400) as u32;
    let (y, m, d) = civil_from_days(days);
    ((y, m, d), (sod / 3600, (sod % 3600) / 60, sod % 60), ms)
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_matches_known_instants() {
        assert_eq!(iso_utc_ms(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(iso_utc_ms(1_788_166_800_000), "2026-08-31T09:00:00.000Z");
        assert_eq!(hms_utc(1_788_166_800_000 + 59_500), "09:00:59");
        assert_eq!(date_utc(951_782_400_000), "2000-02-29");
        assert_eq!(iso_utc_ms(-1), "1969-12-31T23:59:59.999Z");
    }

    #[test]
    fn relative_and_band_strings() {
        assert_eq!(rel_s(59_000), "+59.0s");
        assert_eq!(rel_s(-1_500), "-1.5s");
        assert_eq!(pm_s(0), "±0s");
        assert_eq!(pm_s(250), "±250ms");
        assert_eq!(pm_s(10_000), "±10.0s");
    }
}

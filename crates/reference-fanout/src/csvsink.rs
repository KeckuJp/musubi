//! consumer (v): **汎用 CSV／JSON sink** 2 本。
//!
//! - [`EventCsvPublisher`] … MARK を **専用列**で運ぶ汎用 sink（`status`／`reason_code`／
//!   `confidence`／`basis`／`provenance`／`evidence` の列がある）。ここでは trust-field が
//!   注釈欄でなく本来の列に入るので **可視ダウングレードではない**。
//! - [`LegacyPositionCsvPublisher`] … **位置しか列を持たない** legacy consumer（既存の
//!   「lat/lon だけ取り込む」受け皿の代表）。MARK を一切運べないので、非 OK envelope は
//!   `fan_out` が **WITHHELD** にする（＝黙って位置だけ流さない・contract）。contract の
//!   「運べない sink は WITHHELD」を見せるのがこの sink の役目。
//!
//! contract §3-8 の書き出し規律（unknown を落とさない・順位は表示時 derive と明記）は、
//! reference example 本体の CSV（`reference-pipeline::export`）がそのまま担う。本 module の CSV は
//! **fan-out 台帳**（どの step がどの sink でどうなったか）であって、それを置き換えない。
//!
//! **表記は contract の書き出しと同じ語彙を使う（contract・contract C-2）。** `mark_status` は
//! [`musubi_reference_pipeline::export::mark_label`] の snake_case、digest cell は
//! [`musubi_reference_pipeline::export::DIGEST_PREFIX`] の `sha256:` 接頭辞つき。
//! `musubi-reference-fanout` は本 module の CSV と contract の `absences.csv`／`cause_claims.csv` を
//! **同じ folder へ**書くので、同名の概念（`mark_status`・digest）が file によって別表記だと、
//! 受け取った側が 1 つの folder の中で 2 つの語彙を覚えることになる。

use crate::DemoStep;
use musubi_northbound::{DialectCapability, NorthboundPublisher, PublishError};
use musubi_reference_pipeline::export::{DIGEST_PREFIX, mark_label};
use musubi_types::{ComObject, EvidenceEnvelope};

/// MARK 列を持つ汎用 CSV の方言名。
pub const DIALECT_CSV: &str = "generic-csv";
/// 位置しか運べない legacy CSV の方言名。
pub const DIALECT_LEGACY: &str = "legacy-position-csv";

/// [`EventCsvPublisher`] の列（`export.rs` と同じ「unknown を落とさない」方針）。
pub const CSV_HEADER: &str = "seq,kind,asset_id,marker_id,at_utc,at_unix_ms,lat_deg,lon_deg,alt_m,mark_status,mark_reason_code,confidence,confidence_basis,provenance,evidence_digest8,headline";
/// [`LegacyPositionCsvPublisher`] の列（**MARK 列が無い**のがこの sink の性質）。
pub const LEGACY_HEADER: &str = "marker_id,at_utc,lat_deg,lon_deg";

/// CSV 1 セル（`,`／`"`／改行を含むときだけ引用する）。
#[must_use]
pub fn cell(s: &str) -> String {
    if s.contains([',', '"', '\n', '\r']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_string()
    }
}

/// f64 を固定小数で（科学表記を出さない＝northbound／NDJSON と同じ規律）。
#[must_use]
pub fn num(v: f64) -> String {
    if !v.is_finite() {
        return String::new();
    }
    let s = format!("{v:.7}");
    let t = s.trim_end_matches('0');
    if t.ends_with('.') {
        format!("{t}0")
    } else {
        t.to_string()
    }
}

/// digest cell（[`DIGEST_PREFIX`]＋先頭 8 hex）。`content_digest` が無い step は sentinel `-`。
///
/// # なぜ `export::digest_cell` をそのまま呼ばないか: あちらは `DigestRef`（`[u8; 32]` の newtype）を
/// 取り **先頭 8 byte（16 文字）**を出す。fan-out が持つのは `EvidenceEnvelope.content_digest`＝
/// `Option<[u8; 32]>` で、この列（`evidence_digest8`）は **先頭 4 byte（8 文字）**を出す既存の見せ方。
/// `DigestRef` に包み直して呼ぶと列の**値**が 8 文字から 16 文字へ変わってしまう。contract の指示は
/// 「接頭辞を付ける」であって「digest を変える」ではないので、共有できる部分（接頭辞の定数）だけを
/// import し、組み立ての 1 行だけをここに置く。
///
/// # なぜ CSV 1 層で付けるか（contract と同じ判断）: 危険なのは cell を自動で型付けする取り込み側だけで、
/// CoT `<remarks>` の `last-good:` token・NVG modifiers・NDJSON はそこを通らない。`DemoStep::digest8`
/// と `hex8` は生 hex のまま残し、wire text を 1 byte も変えない。
///
/// # なぜ sentinel `-` に接頭辞を付けないか: それは digest ではない（contract の `never-observed` と同じ扱い）。
#[must_use]
pub fn digest_cell(step: &DemoStep) -> String {
    let d = step.digest8();
    if d == "-" {
        d
    } else {
        format!("{DIGEST_PREFIX}{d}")
    }
}

/// MARK 列を持つ汎用 CSV sink（step ごとに作る）。
#[derive(Debug, Clone, Copy)]
pub struct EventCsvPublisher<'a> {
    step: &'a DemoStep,
}

impl<'a> EventCsvPublisher<'a> {
    #[must_use]
    pub const fn new(step: &'a DemoStep) -> Self {
        Self { step }
    }
}

impl NorthboundPublisher for EventCsvPublisher<'_> {
    fn dialect(&self) -> &str {
        DIALECT_CSV
    }

    fn capability(&self) -> DialectCapability {
        DialectCapability {
            carries_position: true,
            carries_mark_status: true,
            // MARK は専用列に入る（注釈欄への埋め込みではない）。
            mark_via_native_annotation: false,
        }
    }

    fn publish(&self, envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
        let ps = match &envelope.observation {
            ComObject::PlatformState(ps) => ps,
            ComObject::HostStateObservation(_) => {
                return Err(PublishError {
                    dialect: DIALECT_CSV.to_string(),
                    reason: "unsupported host-state observation in position CSV".to_string(),
                });
            }
        };
        let pos = ps.position.as_ref().ok_or_else(|| PublishError {
            dialect: DIALECT_CSV.to_string(),
            reason: "missing position (lat/lon columns are required)".to_string(),
        })?;
        let mark = &envelope.claim.mark;
        let basis = match &envelope.claim.confidence_basis {
            Some(musubi_types::ConfidenceBasis::AdapterAssigned) => "adapter-assigned",
            Some(musubi_types::ConfidenceBasis::Measured { .. }) => "measured",
            None => "unspecified",
        };
        let row = [
            self.step.seq.to_string(),
            self.step.kind.as_str().to_string(),
            cell(&self.step.asset_id),
            cell(&self.step.marker_id),
            musubi_reference_pipeline::timefmt::iso_utc_ms(self.step.at_wall_ms),
            self.step.at_wall_ms.to_string(),
            num(pos.lat_deg),
            num(pos.lon_deg),
            pos.alt_m.map(num).unwrap_or_default(),
            mark_label(mark.status).to_string(),
            cell(&mark.reason_code),
            format!("{:.2}", envelope.claim.confidence),
            basis.to_string(),
            cell(&mark.provenance.join("|")),
            digest_cell(self.step),
            cell(&self.step.headline),
        ]
        .join(",");
        Ok(format!("{row}\n").into_bytes())
    }
}

/// 位置しか列を持たない legacy consumer（MARK を運べない＝非 OK は WITHHELD になる）。
#[derive(Debug, Clone, Copy, Default)]
pub struct LegacyPositionCsvPublisher;

impl LegacyPositionCsvPublisher {
    #[must_use]
    pub const fn new() -> Self {
        Self
    }
}

impl NorthboundPublisher for LegacyPositionCsvPublisher {
    fn dialect(&self) -> &str {
        DIALECT_LEGACY
    }

    fn capability(&self) -> DialectCapability {
        // 位置だけ・MARK 欄なし。`fan_out` はこの宣言を見て非 OK envelope を WITHHELD にする。
        DialectCapability::position_only()
    }

    fn publish(&self, envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
        let ps = match &envelope.observation {
            ComObject::PlatformState(ps) => ps,
            ComObject::HostStateObservation(_) => {
                return Err(PublishError {
                    dialect: DIALECT_LEGACY.to_string(),
                    reason: "unsupported host-state observation in legacy position CSV".to_string(),
                });
            }
        };
        let pos = ps.position.as_ref().ok_or_else(|| PublishError {
            dialect: DIALECT_LEGACY.to_string(),
            reason: "missing position (lat/lon columns are required)".to_string(),
        })?;
        let at = musubi_northbound::effective_time_ms(&ps.timestamps);
        let row = [
            cell(&ps.platform_id),
            musubi_reference_pipeline::timefmt::iso_utc_ms(at),
            num(pos.lat_deg),
            num(pos.lon_deg),
        ]
        .join(",");
        Ok(format!("{row}\n").into_bytes())
    }
}

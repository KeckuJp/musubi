//! 非 MAVLink USV テレメトリ JSON アダプタ（offline＝integrate 境界・contract / contract）。
//!
//! S0/S1 の MAVLink は **wrap**: Musubi が内蔵する標準デコーダ
//! （`adapter-mavlink::decode_global_position_int` の hand-rolled MAVLink2）を呼ぶだけで、
//! アダプタ著者は COM マッピングのみを担う。本 crate は **integrate**: Musubi に標準デコーダが
//! 存在しない独自/非標準 wire 形式（vendor-ish なテキスト JSON `kecku-usv-telemetry/v1`）を、
//! **アダプタ著者自身が parse** して COM の 6 種のいずれか（ここでは `PlatformState`）へ
//! **意味マッピング**する境界である（contract の「非 MAVLink 取り込み境界」）。
//!
//! 設計の核（integrate を wrap に縮退させない）:
//! - 本 crate は **adapter-mavlink を依存に持たない**（MAVLink デコーダを使い回さない）。
//! - JSON は MAVLink shape（degE7 int32 等）に寄せず、`f64` 度・テキスト・vendor 命名で **真に別物**。
//! - parse 経路はテキスト JSON（serde_json）。バイナリ LE little-endian のフレーム解釈ではない。
//!
//! 価値（format 非依存の実証）:
//! - MARK/Evidence は **S1 を書き直さず再利用**する。`musubi_core::{ObservationQuality,
//!   QualityNote, derive_mark, seal_digest}` と `musubi_northbound::CotPublisher` を **そのまま**
//!   呼ぶ＝USV 由来の異種 source が UAV-MAVLink と **同一 canonical node / 同一 COM PlatformState /
//!   同一 MARK・digest・CoT パイプライン**に収束する（heterogeneous-input contract「一度つなぐだけで複数 C2」）。
//!
//! §5 ガードレール:
//! - read-only tap（[`ReadOnlyUsvTap`]・[`UsvManifest`] が egress_bytes=0・heartbeat 無し・
//!   command 無しを宣言＝FEAT-A1-a を policy でなく test で固定）。
//! - serde_json は **この adapter にだけ**置く（core は no-net・zero-外部dep のまま）。
//! - `unsafe` 不在（workspace lint `unsafe_code=forbid`）。MARK を黙って消さない（no-silent-drop）。
//!
//! 参照: the documented interface §Sim 実装ノート L110（USV を Rover/MAVLink 代替に
//! すると integrate が wrap に縮退する→非 MAVLink 合成 source を別途立てる）・07_unknowns.md
//! L42/L75（contract / contract）・the documented interface contract（誤マップ=0・許容誤差 1e-6）。

use musubi_core::{
    NormalizeError, Normalizer, ObservationQuality, QualityNote, RawObservation, derive_mark,
    seal_digest,
};
use musubi_types::{
    Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
};
use serde::Deserialize;

/// アダプタのスラグ（provenance の `adapter:<slug>`・claim_id 接尾辞に使う）。
const ADAPTER_SLUG: &str = "usv-telemetry-json/v1";

/// contract quarantine: 生 JSON payload のバイト上限（DoS 耐性の広い網）。
///
/// 1 テレメトリフレーム分の想定サイズ（既知フィールド＋若干の未知フィールド）に十分な余裕を
/// 持たせた値であって、プロトコル固有の意味検証（gnss_fix/値域等）を置き換えるものではない
/// （`musubi_core::quarantine` の honest scope 参照）。
const MAX_JSON_PAYLOAD_BYTES: usize = 16 * 1024;
/// contract quarantine: `serde_json::from_slice` を呼ぶ**前**の pre-parse 構造走査で許容する
/// JSON ネスト深度上限（stack-overflow 耐性）。本 wire（`UsvTelemetryFrame`）はフラットな
/// オブジェクトなので十分な余裕を持つ。
const MAX_JSON_NESTING_DEPTH: usize = 32;
/// contract quarantine: 単一行識別子フィールド（`vehicle_id`・`mode`）の許容バイト数上限。
const MAX_STR_FIELD_BYTES: usize = 256;

/// time_confidence の数値割当（MAVLink time_boot_ms=0.2 と揃え、閾値 0.5 以下で確実に DEGRADED）。
/// ISO 8601 UTC（絶対時刻・timezone 明示）。
const TIME_CONF_ISO8601_UTC: f32 = 0.85;
/// epoch 秒（絶対時刻だが ISO 形式でない・timezone indicator 無しの ISO もここに落とす）。
const TIME_CONF_EPOCH_SEC: f32 = 0.75;
/// 欠落 / 解析不能（絶対時刻を主張できない＝MAVLink boot-relative 相当の低信頼）。
const TIME_CONF_ABSENT: f32 = 0.2;

/// 1 秒 = 1_000_000 マイクロ秒（observed_at は epoch マイクロ秒 i64）。
const MICROS_PER_SEC: i64 = 1_000_000;

// =====================================================================================
// §5 read-only Manifest ＋ tap（FEAT-A1-a: egress_bytes=0 を Manifest 宣言＋test assert で固定）
// =====================================================================================

/// USV source の read-only Manifest（§5 #1 を policy でなく **宣言された不変条件**として持つ）。
///
/// MAVLink tap（adapter-mavlink）と同じく、Musubi は USV テレメトリを **受動的に取り出す**だけで、
/// 送信（egress）・heartbeat・command を一切出さない。値は **コンパイル時固定**（runtime で変えない）。
/// test がこの不変条件を assert する（FEAT-A1-a の機械保証）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UsvManifest {
    /// 南向きに送出するバイト数。read-only tap ＝ 恒等的に 0（注入・送信をしない）。
    pub egress_bytes: u64,
    /// heartbeat を出すか。read-only ＝ false。
    pub emits_heartbeat: bool,
    /// command/tasking を出すか。read-only ＝ false（X-NOWRITE・contract）。
    pub emits_command: bool,
}

impl UsvManifest {
    /// read-only tap の宣言（全 egress 経路を閉じた既定）。
    #[must_use]
    pub const fn read_only() -> Self {
        Self {
            egress_bytes: 0,
            emits_heartbeat: false,
            emits_command: false,
        }
    }
}

impl Default for UsvManifest {
    fn default() -> Self {
        Self::read_only()
    }
}

/// 受動傍受 USV source の契約（adapter-mavlink の `ReadOnlyTap` と同趣旨）。
///
/// `poll` は次の生 JSON 観測を **取り出す**だけで、注入・送信をしない（read-only）。
/// 双方向制御リンクを持つ USV でも、Musubi 側は subscribe のみ＝publish/command 経路を持たない。
pub trait ReadOnlyUsvTap {
    /// この tap の source 識別子。
    fn source_id(&self) -> &str;

    /// read-only 不変条件の宣言（egress_bytes=0・heartbeat 無し・command 無し）。
    fn manifest(&self) -> UsvManifest;

    /// 次の生観測（JSON テキストバイト列）を取り出す。注入・送信はしない。
    fn poll(&mut self) -> Option<RawObservation>;
}

/// メモリ上の固定キューから JSON 観測を出す read-only tap（sim / test 用の最小実装）。
///
/// 実運用では MQTT subscribe / ROS2 topic / 地上 GCS の JSON 出力に置き換わるが、いずれも
/// **subscribe（受信）のみ**＝この型と同じく送信口を持たない。`poll` はキューを前から消費する
/// だけで、どんな入力に対しても 1 バイトも egress しない。
#[derive(Debug, Clone, Default)]
pub struct QueuedUsvTap {
    source_id: String,
    queue: std::collections::VecDeque<Vec<u8>>,
}

impl QueuedUsvTap {
    /// source 識別子と JSON フレーム列から tap を作る（フレームは受信済みの read-only 入力）。
    #[must_use]
    pub fn new(source_id: impl Into<String>, frames: Vec<Vec<u8>>) -> Self {
        Self {
            source_id: source_id.into(),
            queue: frames.into_iter().collect(),
        }
    }
}

impl ReadOnlyUsvTap for QueuedUsvTap {
    fn source_id(&self) -> &str {
        &self.source_id
    }

    fn manifest(&self) -> UsvManifest {
        // 受信キューを消費するだけ＝送信しない（read-only 不変条件）。
        UsvManifest::read_only()
    }

    fn poll(&mut self) -> Option<RawObservation> {
        // キューから 1 件取り出す read 操作のみ。注入・送信・ack を出さない。
        // received_at: このメモリ上キューは sim/test 用の最小実装で実 ingest 層を持たない
        // （contract・実運用では MQTT subscribe/ROS2 topic の recv 側がここを実受領時刻で埋める。
        // この例の0は実受領時刻を示さない）。
        self.queue.pop_front().map(|payload| RawObservation {
            source_id: self.source_id.clone(),
            payload,
            received_at: 0,
        })
    }
}

// =====================================================================================
// 非 MAVLink wire 形式（vendor-ish JSON）の typed 定義（serde derive で堅く）
// =====================================================================================

/// `kecku-usv-telemetry/v1` の wire フレーム（UTF-8 テキスト JSON・MAVLink バイナリとは別物）。
///
/// `#[serde(deny_unknown_fields)]` は **使わない**: 未知フィールドは黙って弾かず、来歴
/// （`unknown-field:<key>`）に残す（no-silent-drop の精神・spec §フォーマット上のルール）。
/// `null` は「欠落」と同義に扱うため全任意フィールドを `Option` にし、`serde` の既定で None になる。
#[derive(Debug, Clone, Deserialize)]
struct UsvTelemetryFrame {
    /// 機体 ID（COM `platform_id`）。必須。空文字は欠落と同等扱い（INVALID）。
    vehicle_id: Option<String>,
    /// 緯度 度（WGS-84）。必須。
    lat: Option<f64>,
    /// 経度 度（WGS-84）。必須。
    lon: Option<f64>,
    /// 観測時刻の主張（ISO 8601 UTC 文字列 または Unix epoch 秒）。
    timestamp: Option<TimestampField>,
    /// 機首方位（真北・0..360）。欠落 → `field-missing:heading` で DEGRADED 寄与。
    heading_deg: Option<f32>,
    /// 対地速度ノット。narrow spine に枠なし＝中立来歴（欠落しても DEGRADED にしない）。
    sog_knots: Option<f32>,
    /// 高度 m（MSL 基準）。欠落 → `field-missing:alt` で DEGRADED 寄与。
    alt_m: Option<f64>,
    /// 水深 m（USV 固有・将来 PayloadFeed/Track 向け）。中立来歴。
    depth_m: Option<f32>,
    /// GNSS fix 品質（NMEA GGA Quality Indicator 準拠）。0 → INVALID。欠落 → 1 相当＋中立注釈。
    gnss_fix: Option<i64>,
    /// 機体モード（COM `PlatformState.mode` にそのまま渡す）。
    mode: Option<String>,
    /// バッテリ残量 %（将来 Health 向け）。中立来歴。
    battery_pct: Option<i64>,
    /// テレメトリ送出機器/ソフトウェア名（provenance）。中立来歴。
    source_app: Option<String>,
    /// 上記以外の未知フィールド（黙って捨てず来歴に残す＝no-silent-drop）。
    #[serde(flatten)]
    extra: std::collections::BTreeMap<String, serde_json::Value>,
}

/// `timestamp` は文字列（ISO 8601）または数値（epoch 秒）の両方を受ける untagged union。
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
enum TimestampField {
    /// ISO 8601 文字列（`"2024-09-15T03:42:17.123Z"`）または epoch 秒の文字列表現。
    Text(String),
    /// Unix epoch 秒（浮動小数）。
    Epoch(f64),
}

/// 時刻解釈の結果（observed_at の充填と time_confidence・来歴注釈を一括で運ぶ）。
struct TimeInterpretation {
    /// 絶対時刻 epoch マイクロ秒（解釈不能なら None＝observed_at を主張しない）。
    observed_at: Option<i64>,
    /// 時刻信頼度（ISO UTC=0.85 / epoch=0.75 / 欠落・解析不能=0.2）。
    time_confidence: f32,
    /// time に関する中立来歴（黙って消さない）。
    note: QualityNote,
}

// =====================================================================================
// integrate 本体: 非 MAVLink JSON → COM PlatformState 意味マッピング（アダプタ著者の責務）
// =====================================================================================

/// USV テレメトリ JSON → COM `PlatformState` 正規化器（`musubi_core::Normalizer` 実装）。
///
/// 決定論・read-only。テキスト JSON を typed struct へ parse し（integrate＝アダプタ著者の自前
/// parse）、COM の正規フィールドへ意味マッピングする。MARK 導出・seal は **core を再利用**
/// （USV 用に書き直さない＝同一 canonical pipeline に載るのが integrate の価値）。
#[derive(Debug, Clone, Default)]
pub struct UsvNormalizer;

impl UsvNormalizer {
    #[must_use]
    pub fn new() -> Self {
        Self
    }
}

/// INVALID の `NormalizeError` を組み立てる（no-silent-drop・MAVLink adapter の decode 失敗経路と
/// 同じパターン）。`reason_code` と具体的失敗事由（detail）を provenance に保持する。
fn invalid(source_id: &str, reason_code: &str, detail: impl Into<String>) -> NormalizeError {
    NormalizeError {
        source_id: source_id.to_string(),
        mark: Mark {
            status: MarkStatus::Invalid,
            reason_code: reason_code.to_string(),
            provenance: vec![
                format!("source:{source_id}"),
                format!("adapter:{ADAPTER_SLUG}"),
                detail.into(),
            ],
        },
    }
}

/// contract quarantine: [`musubi_core::quarantine::QuarantineReject`] を、`invalid()` と
/// **同型だが別語彙**の [`NormalizeError`] へ写す。reason_code は 3 アダプタ共通の
/// [`musubi_core::quarantine::QUARANTINE_REASON_CODE`]（`"quarantine-rejected"`）を**必ず**
/// 使う（アダプタごとに別語彙を発明しない＝contract の核）。具体的な拒否事由は provenance へ
/// `Display` 経由で残す（no-silent-drop）。
fn quarantine_error(
    source_id: &str,
    reject: musubi_core::quarantine::QuarantineReject,
) -> NormalizeError {
    NormalizeError {
        source_id: source_id.to_string(),
        mark: Mark {
            status: MarkStatus::Invalid,
            reason_code: musubi_core::quarantine::QUARANTINE_REASON_CODE.to_string(),
            provenance: vec![
                format!("source:{source_id}"),
                format!("adapter:{ADAPTER_SLUG}"),
                format!("quarantine:{reject}"),
            ],
        },
    }
}

/// ISO 8601（`CCYY-MM-DDThh:mm:ss[.sss](Z|±hh:mm)`）を epoch マイクロ秒へ。timezone 必須。
///
/// zero-dep・決定論（外部時計に触れない）。proleptic Gregorian（days-from-civil＝Howard
/// Hinnant `days_from_civil`）。timezone indicator（Z または ±HH:MM）が無い形式は **解釈しない**
/// （`None` を返し epoch-sec 相当の低信頼に落とす＝pitfall「timezone 無しは 0.75 以下」）。閏秒は
/// 扱わない（CoT/TAK の慣行どおり UTC を SI 秒近似）。
fn iso8601_utc_to_epoch_micros(s: &str) -> Option<i64> {
    let bytes = s.as_bytes();
    // 最低 "YYYY-MM-DDTHH:MM:SS" = 19 文字 ＋ timezone。
    if bytes.len() < 20 {
        return None;
    }
    // 日付 YYYY-MM-DD。
    let year: i64 = s.get(0..4)?.parse().ok()?;
    if bytes[4] != b'-' {
        return None;
    }
    let month: i64 = s.get(5..7)?.parse().ok()?;
    if bytes[7] != b'-' {
        return None;
    }
    let day: i64 = s.get(8..10)?.parse().ok()?;
    // 日付と時刻の区切りは 'T'（RFC 3339）。
    if bytes[10] != b'T' && bytes[10] != b't' {
        return None;
    }
    let hour: i64 = s.get(11..13)?.parse().ok()?;
    if bytes[13] != b':' {
        return None;
    }
    let minute: i64 = s.get(14..16)?.parse().ok()?;
    if bytes[16] != b':' {
        return None;
    }
    let second: i64 = s.get(17..19)?.parse().ok()?;
    if !(1..=12).contains(&month)
        || !(0..=23).contains(&hour)
        || !(0..=59).contains(&minute)
        || !(0..=60).contains(&second)
    {
        return None;
    }
    // day を**実月長（うるう年考慮）**で検証＝Feb31/Apr31 等の不可能日付を高信頼の絶対時刻に
    // 載せない。不可能日付は None を返し epoch-sec 再試行→ time-suspect/低信頼へ降格する
    // （hand-rolled パーサが day を 1..=31 だけで通し proleptic 算術が次月へ silently 巻き取る
    //  ＝不正日付が「信頼できる observed_at」になる穴を塞ぐ。独立 verifier 2 名指摘）。
    if !(1..=days_in_month(year, month)).contains(&day) {
        return None;
    }

    // 小数秒（任意）＋ timezone。offset 19 から走査する。
    let mut idx = 19usize;
    let mut millis: i64 = 0;
    if idx < bytes.len() && bytes[idx] == b'.' {
        idx += 1;
        let frac_start = idx;
        while idx < bytes.len() && bytes[idx].is_ascii_digit() {
            idx += 1;
        }
        // 先頭 3 桁をミリ秒として採用（足りなければ 0 詰め・余りは切り捨て）。
        let frac = &s[frac_start..idx];
        let mut ms_str = String::with_capacity(3);
        for c in frac.chars().take(3) {
            ms_str.push(c);
        }
        while ms_str.len() < 3 {
            ms_str.push('0');
        }
        millis = ms_str.parse().ok()?;
    }

    // timezone（**必須**）: 'Z'/'z' か ±HH:MM。無ければ解釈不能（None）。
    let tz_offset_secs: i64 = match bytes.get(idx) {
        Some(b'Z' | b'z') => 0,
        Some(c @ (b'+' | b'-')) => {
            let sign = if *c == b'+' { 1 } else { -1 };
            let oh: i64 = s.get(idx + 1..idx + 3)?.parse().ok()?;
            // ±HH:MM（コロンあり）または ±HHMM（コロンなし）を許容。
            let om: i64 = if bytes.get(idx + 3) == Some(&b':') {
                s.get(idx + 4..idx + 6)?.parse().ok()?
            } else {
                s.get(idx + 3..idx + 5)?.parse().ok()?
            };
            sign * (oh * 3600 + om * 60)
        }
        // timezone indicator 無し＝絶対時刻として確定できない（pitfall 対処）。
        _ => return None,
    };

    let days = days_from_civil(year, month as u32, day as u32);
    let secs_utc = days * 86_400 + hour * 3600 + minute * 60 + second - tz_offset_secs;
    Some(secs_utc * MICROS_PER_SEC + millis * 1000)
}

/// 月の日数（proleptic Gregorian・うるう年考慮）。不正月は 0（=どの day も弾く）。
fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
            if leap { 29 } else { 28 }
        }
        _ => 0,
    }
}

/// Howard Hinnant `days_from_civil`（proleptic Gregorian・1970-01-01 を 0 とする日数）。
/// 全演算 i64（northbound::civil_from_days の逆関数に対応・同一アルゴリズム系列）。
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let m = i64::from(m);
    let d = i64::from(d);
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146_097 + doe - 719_468
}

/// `timestamp` フィールドを解釈する（ISO UTC / epoch 秒 / 欠落・解析不能 の 3 系統）。
fn interpret_timestamp(ts: Option<&TimestampField>) -> TimeInterpretation {
    match ts {
        Some(TimestampField::Text(s)) => {
            // まず ISO 8601（timezone 付き）を試す。
            if let Some(micros) = iso8601_utc_to_epoch_micros(s) {
                return TimeInterpretation {
                    observed_at: Some(micros),
                    time_confidence: TIME_CONF_ISO8601_UTC,
                    note: QualityNote::neutral("timestamp-format:iso8601-utc"),
                };
            }
            // 文字列だが ISO で解釈できない場合: epoch 秒の文字列表現を試す。
            if let Ok(epoch) = s.trim().parse::<f64>() {
                if epoch.is_finite() {
                    return epoch_interpretation(epoch);
                }
            }
            // どちらでもない＝解析不能（絶対時刻を主張しない・低信頼）。
            TimeInterpretation {
                observed_at: None,
                time_confidence: TIME_CONF_ABSENT,
                note: QualityNote::neutral("timestamp:absent-or-malformed"),
            }
        }
        Some(TimestampField::Epoch(epoch)) if epoch.is_finite() => epoch_interpretation(*epoch),
        // null / 欠落 / 非有限 → 解析不能（中立来歴に残す）。
        _ => TimeInterpretation {
            observed_at: None,
            time_confidence: TIME_CONF_ABSENT,
            note: QualityNote::neutral("timestamp:absent-or-malformed"),
        },
    }
}

/// epoch 秒（f64）→ epoch マイクロ秒 i64（ISO より一段低い信頼度）。
fn epoch_interpretation(epoch_sec: f64) -> TimeInterpretation {
    // 秒 × 1e6 をマイクロ秒へ（決定論・丸めは切り捨て＝to_bits でなく算術）。
    // f64→i64 の truncation/precision-loss は **意図的**（マイクロ秒未満を捨てる）。
    // i64 範囲（±292,471 年）を超える epoch は実テレメトリで起こらない（飽和は許容）。
    #[allow(
        clippy::cast_possible_truncation,
        clippy::cast_precision_loss,
        clippy::cast_sign_loss
    )]
    let micros = (epoch_sec * MICROS_PER_SEC as f64) as i64;
    TimeInterpretation {
        observed_at: Some(micros),
        time_confidence: TIME_CONF_EPOCH_SEC,
        note: QualityNote::neutral("timestamp-format:epoch-sec"),
    }
}

impl Normalizer for UsvNormalizer {
    fn normalize(&self, raw: &RawObservation) -> Result<EvidenceEnvelope, NormalizeError> {
        let source_id = &raw.source_id;

        // (0) contract quarantine boundary（ガードレール#2 の実装接地点＝the quarantine contract）:
        //     生 payload を大きさ ＋ JSON ネスト深度で検疫する。**`serde_json::from_slice` より
        //     前**に行う＝敵対的に深いネストで recursive-descent parser の stack を消費させる
        //     クラスの入力を、実際に parse を試みる前に決定論的に reject する。
        musubi_core::quarantine::quarantine_payload_size(&raw.payload, MAX_JSON_PAYLOAD_BYTES)
            .map_err(|reject| quarantine_error(source_id, reject))?;
        musubi_core::quarantine::reject_if_json_nesting_exceeds(
            &raw.payload,
            MAX_JSON_NESTING_DEPTH,
        )
        .map_err(|reject| quarantine_error(source_id, reject))?;

        // (1) integrate: アダプタ著者が wire（テキスト JSON）を自前 parse（typed struct へ）。
        //     malformed JSON は黙って落とさず INVALID(json-malformed) で MARK（no-silent-drop）。
        let frame: UsvTelemetryFrame = serde_json::from_slice(&raw.payload)
            .map_err(|e| invalid(source_id, "json-malformed", format!("json-error:{e}")))?;

        // (2) 必須フィールド検証（欠落/空 → INVALID・位置を COM に載せない）。
        let vehicle_id = frame
            .vehicle_id
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                invalid(
                    source_id,
                    "required-field-missing",
                    "missing-field:vehicle_id",
                )
            })?
            .to_string();

        // contract quarantine: `vehicle_id` は COM `platform_id` / `claim_id` に直接写る非信頼
        // 識別子（JSON 文字列＝アダプタ著者が意味検証していない生テキスト）。オーバーサイズや
        // bidi 制御/zero-width 等の危険符号点は黙って通さず reject する（contract の track-aliasing
        // 懸念のフィールド版＝視覚的に同一に見える別バイト列の identity を許さない）。
        musubi_core::quarantine::quarantine_str_field(
            "vehicle_id",
            &vehicle_id,
            MAX_STR_FIELD_BYTES,
        )
        .map_err(|reject| quarantine_error(source_id, reject))?;

        let lat = frame
            .lat
            .ok_or_else(|| invalid(source_id, "required-field-missing", "missing-field:lat"))?;
        let lon = frame
            .lon
            .ok_or_else(|| invalid(source_id, "required-field-missing", "missing-field:lon"))?;
        // lat/lon の値域チェック（WGS-84 範囲外は位置として無効＝INVALID）。
        if !lat.is_finite() || !(-90.0..=90.0).contains(&lat) {
            return Err(invalid(
                source_id,
                "position-out-of-range",
                format!("lat-out-of-range:{lat}"),
            ));
        }
        if !lon.is_finite() || !(-180.0..=180.0).contains(&lon) {
            return Err(invalid(
                source_id,
                "position-out-of-range",
                format!("lon-out-of-range:{lon}"),
            ));
        }

        // (3) gnss_fix==0 → 位置無効として INVALID（位置を COM に載せない・no-silent-drop）。
        //     欠落は 1 相当として扱い「黙って昇格しない」中立注釈を残す。
        let mut notes: Vec<QualityNote> = Vec::new();
        match frame.gnss_fix {
            Some(0) => {
                return Err(invalid(source_id, "gnss-fix-invalid", "gnss_fix:0"));
            }
            Some(fix) => notes.push(QualityNote::neutral(format!("gnss_fix:{fix}"))),
            None => notes.push(QualityNote::neutral("gnss_fix:absent-assumed-1")),
        }

        // (4) 意味マッピング: USV JSON フィールド → COM PlatformState。
        //     alt_m は欠落時 None（COM の Option<f64> に直接対応）＋ DEGRADED 寄与。
        //     alt は MSL 基準（HAE 補正なし）＝中立注釈 "alt-ref:MSL-assumed" を必ず積む。
        let alt_m = frame.alt_m;
        if alt_m.is_some() {
            notes.push(QualityNote::neutral("alt-ref:MSL-assumed"));
        } else {
            notes.push(QualityNote::degraded(
                "alt_m field absent",
                "field-missing:alt",
            ));
        }

        // heading_deg は narrow spine（PlatformState）に枠が無い。提供時は中立来歴、欠落時は
        // DEGRADED 寄与（MAVLink の hdg=UINT16_MAX→field-missing:hdg と同じ扱い）。
        if let Some(hd) = frame.heading_deg {
            notes.push(QualityNote::neutral(format!("heading_deg:{hd}")));
        } else {
            notes.push(QualityNote::degraded(
                "heading_deg field absent",
                "field-missing:heading",
            ));
        }

        // sog_knots は narrow spine に枠なし＝中立来歴（欠落しても意味が変わらない＝DEGRADED 対象外）。
        if let Some(sog) = frame.sog_knots {
            notes.push(QualityNote::neutral(format!("sog_knots:{sog}")));
        }
        // depth_m / battery_pct / source_app は将来増分向け / provenance＝中立来歴（黙って消さない）。
        if let Some(depth) = frame.depth_m {
            notes.push(QualityNote::neutral(format!("depth_m:{depth}")));
        }
        if let Some(batt) = frame.battery_pct {
            notes.push(QualityNote::neutral(format!("battery_pct:{batt}")));
        }
        if let Some(app) = &frame.source_app {
            notes.push(QualityNote::neutral(format!("source-app:{app}")));
        }
        // 未知フィールドは黙って無視せず来歴に残す（no-silent-drop）。決定論順序（BTreeMap）。
        for key in frame.extra.keys() {
            notes.push(QualityNote::neutral(format!("unknown-field:{key}")));
        }

        // (5) 時刻解釈（ISO UTC=0.85 / epoch=0.75 / 欠落=0.2）。time の来歴注釈を積む。
        let time = interpret_timestamp(frame.timestamp.as_ref());
        notes.push(time.note);

        // contract: `Claim.confidence` は下で adapter が定数 0.6 を付す（上流 wire に confidence
        // フィールドは存在しない＝Musubi 自身の claim-level assertion であって伝播値ではない）。
        // この由来を黙って隠さず provenance に明示する（"confidence:adapter-assigned"）。
        // ⚠️ Claim 型に origin discriminator（wire 由来 vs adapter 既定）を持たせる Option 化は
        // `crates/types` 側の変更が要るため本 crate の scope 外＝provenance marker 方式を採る
        // （ROUTE の二択のうち後者。adapter-mavlink も同一パターンで揃えることを推奨）。
        notes.push(QualityNote::neutral("confidence:adapter-assigned"));

        // contract quarantine: `mode` も非信頼 JSON 由来の自由文字列（COM `PlatformState.mode`
        // にそのまま転記される）。present のときだけ検疫する（欠落は既に DEGRADED 経路が別途扱う）。
        if let Some(mode) = frame.mode.as_deref() {
            musubi_core::quarantine::quarantine_str_field("mode", mode, MAX_STR_FIELD_BYTES)
                .map_err(|reject| quarantine_error(source_id, reject))?;
        }

        // (6) COM PlatformState を組み立てる（UAV-MAVLink と同一の型・同一フィールド定義へ収束）。
        let position = Position {
            lat_deg: lat,
            lon_deg: lon,
            alt_m,
        };
        let timestamps = Timestamps {
            observed_at: time.observed_at,
            // contract: ingest 層（transport/tap）が刻んだ受領時刻をそのまま転記する
            // （以前は adapter が `0` を決め打ちしていた＝二重時刻が構造的に潰れる原因だった）。
            received_at: raw.received_at,
            time_confidence: time.time_confidence,
        };
        // regression review（platform_id のクロスアダプタ/クロスベンダー
        // 衝突＝track aliasing）: `vehicle_id` はデバイス自称の raw ID で namespace が無く、複数
        // ベンダー/複数艦隊が同種アダプタを別 tap で走らせると異なる 2 隻の物理 USV が北向きで
        // 「同一プラットフォーム」に静かに融合しうる。adapter-mavlink が contract で
        // `{source_id}/sys{sysid}` へ demux 済みなのと揃え、tap 単位で一意な `source_id`
        // （このアダプタインスタンスの識別子）を namespace prefix として合成する。
        let platform_id = format!("{source_id}/{vehicle_id}");
        let platform = PlatformState {
            platform_id: platform_id.clone(),
            position: Some(position),
            mode: frame.mode,
            timestamps,
            // contract(b): この adapter は USV テレメトリ専用（このモジュール自体が「USV JSON
            // 正規化器」）＝Manifest 由来のドメイン宣言は `Surface` で確定。
            platform_domain: musubi_types::PlatformDomain::Surface,
        };

        // (7) MARK は core を再利用して決定論導出（USV 用に書き直さない＝format 非依存の実証）。
        let quality = ObservationQuality {
            source_id: source_id.clone(),
            adapter_slug: ADAPTER_SLUG.to_string(),
            time_confidence: time.time_confidence,
            notes,
        };
        let mark = derive_mark(&quality);
        let claim = Claim {
            // MAVLink の "{platform_id}:global_position_int" に倣う（candidate A-2 是正で
            // namespace 付き platform_id 起点へ揃える。以前は vehicle_id 直＝チャネル/艦隊を
            // またいだ衝突時に claim_id も一緒に衝突していた）。
            claim_id: format!("{platform_id}:usv-telemetry-json"),
            // 観測の主張であって確定でない（control プレーンが自動採用しない）。
            // contract: 上流 USV wire に confidence フィールドは無い＝この 0.6 は adapter が
            // 付す既定値（捏造ではなく明示の Musubi 既定＝provenance に "confidence:adapter-assigned"
            // が必ず伴う。上と cross-vendor で数値が揃っている理由もないので比較には使わないこと）。
            confidence: 0.6,
            mark,
            // contract: adapter 既定値の由来を型でも明示（provenance の "confidence:adapter-assigned" と対）。
            confidence_basis: Some(musubi_types::ConfidenceBasis::AdapterAssigned),
        };

        // (8) fan-out 手前の単一正本を確定（content digest 充填・contract base）。signature は None。
        //     seal_digest も core をそのまま再利用＝UAV と同一の sealed envelope 構造。
        //     contract §3.5 予約フィールドは source preview 未設定＝None（canonical_bytes 除外＝golden 不変）。
        Ok(seal_digest(EvidenceEnvelope {
            observation: ComObject::PlatformState(platform),
            claim,
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }))
    }
}

#[cfg(test)]
mod usv_integrate;

#[cfg(test)]
mod heterogeneity;

#[cfg(test)]
mod north_star_2x2;

#[cfg(test)]
mod quarantine_tests;

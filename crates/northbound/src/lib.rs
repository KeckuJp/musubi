//! Offline CoT/NVG/NDJSON serialization and deterministic fanout decisions.
//! These APIs return bytes and accounting; they do not open a network connection.
//! Synthetic output checks do not establish acceptance by an external consumer.

use musubi_types::{ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformDomain, Timestamps};

pub mod cot_policy;
pub mod fanout;
mod host_state_json;
pub mod nvg_policy;
mod wire_policy;

pub use host_state_json::{EvidenceNdjsonPublisher, MaintenanceHostStateNdjsonPublisher};

/// 直列化失敗（受理点の必須フィールド欠落等）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishError {
    pub dialect: String,
    pub reason: String,
}

/// C2 方言が **trust-critical フィールドをどう運べるか**の能力宣言（contract 段階化の判定表）。
///
/// fan-out（[`fanout`]）は各 sink の本宣言を見て「通常 publish / 可視ダウングレード / WITHHELD」を
/// 決める（the documented interface contract WHERE・the documented interface C-5）。これは **read-only な記述**であって
/// 書き込み経路ではない（§5 X-NOWRITE と無関係＝能力を述べるだけ）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DialectCapability {
    /// 位置（lat/lon）を運べるか（CoT/NVG とも true）。
    pub carries_position: bool,
    /// MARK の status / reason_code を **何らかの形で**運べるか
    /// （ネイティブ MARK 欄、または方言ネイティブ注釈欄への可視ダウングレードを含む）。
    ///
    /// `false`（MARK を全く運べない sink）＋ 非 OK envelope → WITHHELD（fail-closed 既定）。
    pub carries_mark_status: bool,
    /// MARK を運ぶ手段が「方言ネイティブ注釈欄への文字列埋め込み（可視ダウングレード）」か。
    ///
    /// `true`＝MARK 専用欄ではなく注釈欄（CoT `<remarks>` / NVG `label`+`modifiers`）に
    /// 載せる＝[`fanout::SinkOutcome::PublishedWithDowngrade`] になる。
    pub mark_via_native_annotation: bool,
}

impl DialectCapability {
    /// 位置も MARK も運べない sink（stripped/位置のみの最小 C2）。fan-out で WITHHELD 判定の対象。
    #[must_use]
    pub const fn position_only() -> Self {
        Self {
            carries_position: true,
            carries_mark_status: false,
            mark_via_native_annotation: false,
        }
    }
}

/// 北向きコネクタ（C2 方言への serializer）。read-only publish のみ。
pub trait NorthboundPublisher {
    /// この serializer が出す C2 方言名（例: `"cot"`, `"nvg"`, `"mip"`）。
    fn dialect(&self) -> &str;

    /// この方言の trust-critical フィールド運搬能力（contract 段階化の判定表）。
    ///
    /// 既定は「位置 ＋ MARK を方言ネイティブ注釈欄で運べる（可視ダウングレード）」。
    /// CoT は `<remarks>`、NVG は `label`+`modifiers` に MARK を載せる実装を持つため、
    /// 両 publisher はこの既定を採る。MARK を一切運べない sink は本メソッドを override する。
    fn capability(&self) -> DialectCapability {
        DialectCapability {
            carries_position: true,
            carries_mark_status: true,
            mark_via_native_annotation: true,
        }
    }

    /// Evidence Envelope を C2 方言へ直列化して publish 用バイト列にする。
    ///
    /// # Errors
    /// 直列化に失敗した場合（必須フィールド欠落等）に [`PublishError`] を返す。
    fn publish(&self, envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError>;
}

/// MARK status の人間可読な短い tag（CoT `<remarks>` / NVG `label` に出す可視文字列）。
///
/// contract 可視ダウングレード: MARK を方言ネイティブ欄へ「運べる形に変換して保持」する中核。
/// `status`（enum 名）＋ `reason_code` を 1 行に畳む（受信 C2 の地図 UI 上に出る）。
#[must_use]
/// `pub(crate)`: `nvg_policy` の allow-list が serializer と一致することを
/// 機械確認する drift-guard test から参照する（serializer policy）。公開面は増やさない。
pub(crate) fn mark_status_label(status: MarkStatus) -> &'static str {
    match status {
        MarkStatus::Ok => "OK",
        MarkStatus::Degraded => "DEGRADED",
        MarkStatus::Invalid => "INVALID",
        MarkStatus::OrderUnknown => "ORDER_UNKNOWN",
        MarkStatus::Withheld => "WITHHELD",
        MarkStatus::StaleRevocation => "STALE_REVOCATION",
        // 予約・未発火（contract §3.3）: CoT `<remarks>` / NVG `label`+`modifiers` の方言ネイティブ欄に
        // 出す可視ラベル。derive_mark は返さないため source preview の出力には現れない。
        MarkStatus::SelfAssertedTime => "SELF_ASSERTED_TIME",
        // 予約・未発火（contract）: store-and-forward の bounded buffer 飽和で退避が起きたことを示す。
        MarkStatus::BufferSaturated => "BUFFER_SATURATED",
    }
}

/// 北向きに出す基準時刻を選ぶ（epoch **ミリ秒**・contract）。
///
/// `observed_at`（epoch **マイクロ秒**・adapter-usv-json/adapter-template の既存規約）があれば
/// それを ms へ変換して**優先**する（機体/センサーが主張する真の観測時刻の方が運用上意味がある）。
/// 無ければ `received_at`（epoch ms・Musubi 受領時刻）へフォールバックする
/// （MAVLink S0 のように絶対時刻を主張できない場合の唯一の手がかり）。
///
/// ⚠️ unit 契約（contract の µs/ms skew 修正）: `observed_at` を変換せず ms として直接使うと
/// 桁が 1000 倍ずれて日時が壊れる（独立 verifier が指摘した latent hole）。ここで必ず
/// `div_euclid(1000)` を通す。`musubi_types::Timestamps` のフィールド doc も参照。
#[must_use]
pub fn effective_time_ms(t: &Timestamps) -> i64 {
    match t.observed_at {
        Some(observed_at_us) => observed_at_us.div_euclid(1000),
        None => t.received_at,
    }
}

/// MARK を `status` enum 名 + reason_code で短く要約（例: `"DEGRADED:time-suspect"`）。
#[must_use]
fn mark_summary(mark: &Mark) -> String {
    if mark.reason_code.is_empty() {
        mark_status_label(mark.status).to_string()
    } else {
        format!("{}:{}", mark_status_label(mark.status), mark.reason_code)
    }
}

// === CoT (Cursor on Target) publisher ===

/// 精度不明の sentinel（CoT spec: ce/le は「精度不明」を 9999999.0 で表す・"悪い"ではない）。
const CE_LE_UNKNOWN: f64 = 9_999_999.0;

/// grill-traced 設定値: 北向き stale TTL（DDIL 遅延配送の鮮度意味論・**contract 是正**）。
///
/// the documented interface S-3「stale TTL を裸の運用定数としてコードに置かない」の実装追従。
/// 裸の `const STALE_TTL_SECS: i64 = 15` を置換し、COM オブジェクト種別ごとに TTL を
/// 宣言できるようにする（S0 は `PlatformState` のみ実装。他 COM 種は該当 increment で
/// フィールドを追加する＝god-model 化を避け narrow に保つ）。要件トレース: RTM 行は
/// `the documented interface`（contract）。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StaleTtlConfig {
    /// `PlatformState`（1 Hz UAV/USV track）の stale TTL（秒）。
    ///
    /// 既定 15s は CoT spec の例値（1 Hz トラックの典型）＝旧 `STALE_TTL_SECS` と同値で
    /// S0 golden に回帰を起こさない。呼び出し側（application integration 等）が機体クラス/
    /// リンク特性に応じて明示的に上書きできる。
    pub platform_state_secs: i64,
}

impl StaleTtlConfig {
    #[must_use]
    pub const fn new(platform_state_secs: i64) -> Self {
        Self {
            platform_state_secs,
        }
    }

    /// the documented interface S-3 の既定値（1 Hz トラック典型・CoT spec 例=+15s）。
    #[must_use]
    pub const fn recommended() -> Self {
        Self::new(15)
    }

    /// COM オブジェクト種別に応じた TTL 秒（per-type・contract）。
    fn secs_for(&self, observation: &ComObject) -> i64 {
        match observation {
            ComObject::PlatformState(_) => self.platform_state_secs,
            ComObject::HostStateObservation(_) => self.platform_state_secs,
        }
    }
}

impl Default for StaleTtlConfig {
    fn default() -> Self {
        Self::recommended()
    }
}

/// DDIL 遅延配送（DENIED 復旧後のバックログ drain）を示す reason_code トークン
/// （late-but-valid MARK 語彙・**contract 是正**）。
///
/// 呼び出し側（store-and-forward drain 後の scenario/fan-out 層）が、drain した envelope の
/// `Mark.reason_code` へ本トークンを合成する（他 reason token と同じく `"|"` 連結・contract
/// ワイヤ文法契約に従う）ことで、northbound の remarks/modifiers（[`mark_summary`]）に
/// **無言で消えず**現れる（no-silent-stale・the documented interface S-3／the documented interface contract WHEN）。
/// [`is_late_delivery`] で判定した envelope にのみ付与すること。
///
/// honest scope: 本 crate は「配信時刻で鮮度を偽装しない」ための語彙とその判定関数を提供
/// するのみ——実際に store-and-forward drain 経路（`application integration`）へ配線し
/// `Mark.reason_code` へ合成する処理自体は本 fix のスコープ外（northbound crate 単体の
/// 是正）であり、別途配線が必要。
pub const LATE_DELIVERY_REASON_CODE: &str = "late-delivery";

/// 遅延配送の判定（late-but-valid MARK 語彙・**contract 是正**）。
///
/// [`effective_time_ms`] が返す実時刻に `ttl_secs` を足した stale 境界を、実際の配信
/// （drain/publish）時刻 `delivered_at_ms` が超えていれば `true`。**配信時刻で鮮度を
/// 偽装しない契約**（the documented interface S-3・the documented interface contract WHEN）を northbound 単体で
/// 検証可能にする純粋関数（wall-clock を読まない＝決定論・呼び出し側が drain 時刻を渡す。
/// `core::store_and_forward` の `DrainedItem.order_unknown` と同型の side-channel 判定）。
#[must_use]
pub fn is_late_delivery(effective_time_ms: i64, ttl_secs: i64, delivered_at_ms: i64) -> bool {
    delivered_at_ms > effective_time_ms + ttl_secs * 1000
}

/// platform domain（Air/Surface/Ground）→ CoT battle dimension 文字（**contract(b) 是正**）。
///
/// `Unknown`（ドメイン未宣言）は `None` を返す——呼び出し側はこの場合 battle dimension を
/// **省略した短い type**（`"a-u"`）を使う。CoT の階層的 type 表現では、より深い階層を
/// 省略することが「それ以上specifyしない」を正しく表す（既知の 3 ドメインいずれかへ
/// **黙って解決してはいけない** — これが contract(b) の核心＝「空の UAV が友軍地上部隊として
/// 描画される」の再発防止）。
#[must_use]
fn cot_battle_dimension(domain: PlatformDomain) -> Option<&'static str> {
    match domain {
        PlatformDomain::Air => Some("A"),
        PlatformDomain::Surface => Some("S"),
        PlatformDomain::Ground => Some("G"),
        PlatformDomain::Unknown => None,
        // Publisher rejects this unqualified symbol mapping before formatting.
        PlatformDomain::Subsurface => None,
    }
}

/// CoT `event@type` を組み立てる（**contract(b) 是正**）。
///
/// affiliation は既定で `u`（Unknown）に固定する——監査 ROUTE の指摘（northbound の
/// コメント:150 は「affiliation 判定は上位 downstream consumer の責務」と言いながら、実装は自ら
/// `f`（友軍）を断定していた内部矛盾）を解消する。Musubi は**ドメイン**（Air/Surface/
/// Ground）は運べるが、affiliation（敵味方）は運ばない。
fn cot_type(domain: PlatformDomain) -> String {
    match cot_battle_dimension(domain) {
        Some(dim) => format!("a-u-{dim}"),
        None => "a-u".to_string(),
    }
}

/// CoT event XML serializer（TAK 方言）。EvidenceEnvelope → `<event><point/><detail/></event>`。
///
/// S0 マッピング（COT SPEC の mapping_to_musubi に準拠）:
/// - `event@uid`   ← `PlatformState.platform_id`
/// - `point@lat`   ← `position.lat_deg`（WGS-84・無変換）
/// - `point@lon`   ← `position.lon_deg`
/// - `point@hae`   ← `position.alt_m`（※S0 は MSL をそのまま hae に載せる。EGM ジオイド補正は
///                    後続 increment＝altitude_reference を Manifest 宣言させてから）
/// - `point@ce/le` ← 9999999.0（S0 は精度メタデータ未取得）
/// - `event@type`  ← [`cot_type`]（`PlatformState.platform_domain` 由来・**contract(b)**。
///                    affiliation は `u`＝Musubi は判定しない。旧実装は `a-f-G` 無条件）
/// - `event@how`   ← `m-g`（機器 GNSS から機械取得）
///
/// **S3（contract 可視ダウングレード）**: CoT に MARK 専用欄は無いため、`<detail>` 配下の
/// `<remarks>` 要素に MARK summary（`status:reason_code`）と confidence を載せて運ぶ
/// （TAK/ATAK が remarks をテキスト表示する＝可視ダウングレード）。これにより
/// trust-critical フィールド（MarkStatus/reason_code/confidence）が CoT 出力から
/// **黙って消えない**（no-silent-drop）。MARK は CoT に hard-bind されない＝Musubi の
/// 出し分け表現であって暗号強制ではない（tamper-evident, not tamper-proof）。
///
/// remarks の wire 文法（トップレベル `;` 区切り 4 フィールド）:
/// `MARK:{status:reason_code};CONF:{f32};PROV:{a|b|c};BASIS:{adapter-assigned|measured|unspecified}`。
/// **BASIS**（contractb-2）は confidence の由来で、下流 C2 が adapter 既定値を実測値と誤認して
/// cross-vendor 比較するのを防ぐ。**`calibration_id` は北向きに出さない**（`measured` の有無だけ）。
#[derive(Debug, Clone, Copy, Default)]
pub struct CotPublisher {
    /// per-type stale TTL 設定（**contract**・既定 [`StaleTtlConfig::recommended`]）。
    ttl: StaleTtlConfig,
}

impl CotPublisher {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// per-type TTL 設定を明示指定して構築する（**contract**: 裸定数からの置換）。
    #[must_use]
    pub const fn with_ttl(ttl: StaleTtlConfig) -> Self {
        Self { ttl }
    }
}

impl NorthboundPublisher for CotPublisher {
    fn dialect(&self) -> &str {
        "cot"
    }

    fn publish(&self, envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
        let err = |reason: &str| PublishError {
            dialect: "cot".to_string(),
            reason: reason.to_string(),
        };

        let ps = match &envelope.observation {
            ComObject::PlatformState(ps) => ps,
            ComObject::HostStateObservation(_) => {
                return Err(err("unsupported host-state observation for positional CoT"));
            }
        };
        if ps.platform_domain == PlatformDomain::Subsurface {
            return Err(err("subsurface domain has no qualified CoT mapping"));
        }

        let pos = ps
            .position
            .as_ref()
            .ok_or_else(|| err("missing position (lat/lon required by CoT point)"))?;

        // 時刻: contract — observed_at（µs→ms 変換）があれば優先、無ければ received_at（ms）に
        // フォールバックして UTC ISO 8601 に。start=time、stale=time+TTL（per-type・contract）。
        // 決定論（外部時計に触らない）。
        let t_ms = effective_time_ms(&ps.timestamps);
        let time = iso8601_utc_from_epoch_ms(t_ms);
        let ttl_secs = self.ttl.secs_for(&envelope.observation);
        let stale = iso8601_utc_from_epoch_ms(t_ms + ttl_secs * 1000);

        // hae: alt_m が無ければ高度不明 sentinel（CoT は hae 必須）。
        let hae = pos.alt_m.unwrap_or(CE_LE_UNKNOWN);

        // contract(b): event@type は platform_domain（Manifest 由来）から組み立てる
        // （旧実装は `a-f-G` 無条件＝全機体が友軍地上部隊として描画される bug）。
        let cot_type_attr = cot_type(ps.platform_domain);

        // contract 可視ダウングレード: MARK と confidence を <remarks> に運ぶ（黙って落とさない）。
        // remarks は要素テキストなので最小エスケープ（& < > " ）を施す（xml_escape_attr 流用）。
        let mark = &envelope.claim.mark;
        // contractb-2: confidence の由来（BASIS）も可視ダウングレードに載せる（生値だけ出して由来を
        // 落とすと下流が実測値と誤認しうる）。末尾 append＝既存の位置依存 split を最小限に保つ。
        let remarks_text = format!(
            "MARK:{summary};CONF:{conf};PROV:{prov};BASIS:{basis}",
            summary = mark_summary(mark),
            conf = fmt_conf(envelope.claim.confidence),
            prov = mark.provenance.join("|"),
            basis = fmt_basis(envelope.claim.confidence_basis.as_ref()),
        );
        let remarks = xml_escape_attr(&remarks_text);

        // XML 手組み（zero-dep）。uid は属性なので最小エスケープ（& < > " ）を施す。
        let uid = xml_escape_attr(&ps.platform_id);
        let xml = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
             <event version=\"2.0\" uid=\"{uid}\" type=\"{cot_type_attr}\" how=\"m-g\" \
             time=\"{time}\" start=\"{time}\" stale=\"{stale}\">\n  \
             <point lat=\"{lat}\" lon=\"{lon}\" hae=\"{hae}\" ce=\"{ce}\" le=\"{le}\"/>\n  \
             <detail>\n    <remarks>{remarks}</remarks>\n  </detail>\n\
             </event>\n",
            uid = uid,
            cot_type_attr = cot_type_attr,
            time = time,
            stale = stale,
            lat = fmt_f64(pos.lat_deg),
            lon = fmt_f64(pos.lon_deg),
            hae = fmt_f64(hae),
            ce = fmt_f64(CE_LE_UNKNOWN),
            le = fmt_f64(CE_LE_UNKNOWN),
            remarks = remarks,
        );

        // serializer policy construction gate: 送出前に **組み立てた bytes から独立に再導出**して policy を通す
        // （serializer の内部変数を信用しない＝teeth）。宣言外の type／detail・壊れた remarks 文法は
        // ここで `PublishError` になり、`fanout::fan_out` の `SinkOutcome::Failed` として理由付きで
        // 会計される（no-silent-drop・per-publisher エラー隔離は不変）。
        cot_policy::check_cot_event_bytes(xml.as_bytes())
            .map_err(|violation| err(&format!("cot-policy:{violation}")))?;

        Ok(xml.into_bytes())
    }
}

/// f64 を CoT 属性向けに整形。**固定小数表記**（科学表記を避ける）。
///
/// Debug `{:?}` は |x|<1e-4 を指数形（例 "5e-5"）にするため、赤道/本初子午線の ~11 m 以内の
/// 座標で CoT が壊れ、十進を期待する TAK/ATAK/pytak の parser が弾く。degE7 分解能=1e-7 に
/// 合わせ 7 桁・末尾ゼロ除去（小数点以下は最低 1 桁残す＝"488"でなく"488.0"）。NaN/Inf は sentinel。
fn fmt_f64(v: f64) -> String {
    if !v.is_finite() {
        // 非有限は CoT に出さない（sentinel に落とす＝no-silent-bad-number）。
        return "9999999.0".to_string();
    }
    let s = format!("{v:.7}");
    let trimmed = s.trim_end_matches('0');
    if trimmed.ends_with('.') {
        format!("{trimmed}0")
    } else {
        trimmed.to_string()
    }
}

/// f32 confidence を可視欄向けに整形（2 桁固定小数・科学表記回避）。MARK summary に使う。
fn fmt_conf(v: f32) -> String {
    if v.is_finite() {
        format!("{v:.2}")
    } else {
        "0.00".to_string()
    }
}

/// `confidence_basis` を可視欄向けの短い tag（`BASIS` トークン）に整形する（contractb-2）。
///
/// confidence の生値だけを北向きに出すと、下流 C2（TAK/ATAK/Delta 等）が adapter 既定値の 0.6 を
/// 実測値と誤認して cross-vendor 比較しうる（アダプタが繰り返し警告する失敗モード）。由来を可視化して
/// この誤認の余地を消す（no-silent-drop 規律との一貫）。
/// - **`calibration_id` は北向きに出さない**（contractb-2）＝較正台帳 ID を外部 C2 に曝さず開示面積を最小化。
///   `Measured` の**有無**だけを運ぶ。
/// - `None` は「規約上 `AdapterAssigned` 相当だが由来が明示されていない」＝`unspecified`（honest＝
///   `measured` と誤認させない・by-wire で常に BASIS トークンが存在する）。
fn fmt_basis(basis: Option<&musubi_types::ConfidenceBasis>) -> &'static str {
    match basis {
        Some(musubi_types::ConfidenceBasis::AdapterAssigned) => "adapter-assigned",
        Some(musubi_types::ConfidenceBasis::Measured { .. }) => "measured",
        None => "unspecified",
    }
}

/// XML 属性値／要素テキストの最小エスケープ（& < > " のみ・uid/remarks/label 用）。
fn xml_escape_attr(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(c),
        }
    }
    out
}

/// epoch ミリ秒 → ISO 8601 UTC（`CCYY-MM-DDThh:mm:ss.sssZ`）。zero-dep・決定論。
///
/// proleptic Gregorian（days-from-civil・Howard Hinnant アルゴリズム）。閏秒は扱わない
/// （CoT/TAK の慣行どおり UTC を SI 秒として近似）。
fn iso8601_utc_from_epoch_ms(epoch_ms: i64) -> String {
    let ms = epoch_ms.rem_euclid(1000);
    let total_secs = epoch_ms.div_euclid(1000);
    let secs_of_day = total_secs.rem_euclid(86_400);
    let days = total_secs.div_euclid(86_400); // 1970-01-01 からの日数（負も可）。

    let hour = secs_of_day / 3600;
    let minute = (secs_of_day % 3600) / 60;
    let second = secs_of_day % 60;

    let (year, month, day) = civil_from_days(days);

    format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{ms:03}Z",
        year = year,
        month = month,
        day = day,
        hour = hour,
        minute = minute,
        second = second,
        ms = ms,
    )
}

/// days-from-civil の逆（epoch 日数 → (year, month, day)）。Hinnant `civil_from_days`。
/// 全演算を i64 で行い、`month`/`day` のみ確定境界（[1,12]/[1,31]）で u32 へ。
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = if month <= 2 { y + 1 } else { y };
    (year, month as u32, day as u32)
}

// === NVG 2.0.2 (NATO Vector Graphics) publisher ===

// NVG namespace URI と version 文字列は [`nvg_policy`] が宣言語彙として持つ正本を使う
// （serializer と policy で literal を二重に持たない＝serializer policy で drift 面を 1 つ減らす）。
use nvg_policy::{NVG_NS, NVG_VERSION};

/// APcontractB SIDC の affiliation 文字（既定 `U`=Unknown・**contract(b) 是正**）。
///
/// CoT の [`cot_type`] と同じ是正: Musubi は affiliation（敵味方）を判定しない
/// （上位 downstream consumer の責務）ため、常に Unknown を出す。旧実装は `F`（友軍）を無条件断定
/// していた（CoT `a-f-G` と対の bug）。
const NVG_AFFILIATION_UNKNOWN: char = 'U';

/// platform domain（Air/Surface/Ground）→ APcontractB SIDC の Battle Dimension 文字
/// （**contract(b) 是正**）。
///
/// `Unknown`（ドメイン未宣言）は `'-'`（SIDC の「フィールド未指定」規約・同じ symbol 文字列
/// 内の function-id 未使用フィールドと同じ dash 記法）を返す——既知の 3 ドメインいずれかへ
/// **黙って解決してはいけない**（[`cot_battle_dimension`] と同じ契約）。
#[must_use]
fn app6b_battle_dimension(domain: PlatformDomain) -> char {
    match domain {
        PlatformDomain::Air => 'A',
        PlatformDomain::Surface => 'S',
        PlatformDomain::Ground => 'G',
        PlatformDomain::Unknown => '-',
        // Publisher rejects this unqualified symbol mapping before formatting.
        PlatformDomain::Subsurface => '-',
    }
}

/// NVG `point@symbol`（APcontractB SIDC）を platform domain から組み立てる（**contract(b) 是正**）。
///
/// 旧実装は `app6b:SFGP------*****`（friend+ground）を全 platform に無条件出力していた。
#[must_use]
fn app6b_symbol(domain: PlatformDomain) -> String {
    format!(
        "app6b:S{aff}{dim}P------*****",
        aff = NVG_AFFILIATION_UNKNOWN,
        dim = app6b_battle_dimension(domain),
    )
}

/// NVG 2.0.2 位置レポート serializer（NATO Vector Graphics 方言）。
/// EvidenceEnvelope → `<nvg><point/></nvg>`（2nd C2 方言・S3）。
///
/// マッピング（S-1 北向き NVG 2.0.2 公式ベースライン準拠・NVG FORMAT spec §2）:
/// - `point@uri`       ← `PlatformState.platform_id`（xml_escape_attr）
/// - `point@x`         ← `position.lon_deg`（**NVG は x=経度**＝CoT の lat/lon と逆順・要注意）
/// - `point@y`         ← `position.lat_deg`（**NVG は y=緯度**）
/// - `point@z`         ← `position.alt_m`（`None` なら属性ごと省略）
/// - `point@symbol`    ← [`app6b_symbol`]（`PlatformState.platform_domain` 由来・**contract(b)**。
///                        affiliation は `U`＝Musubi は判定しない。旧実装は `SFGP------*****` 無条件）
/// - `point@label`     ← `platform_id` + MARK summary（contract 可視ダウングレード・人間可読）
/// - `point@modifiers` ← `MARK:status;REASON:code;CONF:confidence;BASIS:{adapter-assigned|measured|unspecified}`
///                        （機械可読 MARK 搬送。**BASIS**＝confidence の由来・contractb-2・`calibration_id` は出さない）
/// - `TimeStamp@when`  ← `effective_time_ms(timestamps)`（observed_at 優先・無ければ received_at・
///                        ISO 8601 UTC・CoT と同じ変換を流用・contract）
///
/// honest scope: 公開 XSD（anesdi73/nvg-parser）に conformant な **self-attest golden** であって
/// 実 Delta / NATO NVG impl が受理した証明ではない（external validation・contract）。MARK は NVG ネイティブ欄
/// を持たず label/modifiers への可視ダウングレードで運ぶ＝受信側が解釈するかは実装依存。
#[derive(Debug, Clone, Default)]
pub struct NvgPublisher;

impl NvgPublisher {
    #[must_use]
    pub fn new() -> Self {
        Self
    }
}

impl NorthboundPublisher for NvgPublisher {
    fn dialect(&self) -> &str {
        "nvg"
    }

    fn publish(&self, envelope: &EvidenceEnvelope) -> Result<Vec<u8>, PublishError> {
        let err = |reason: &str| PublishError {
            dialect: "nvg".to_string(),
            reason: reason.to_string(),
        };

        let ps = match &envelope.observation {
            ComObject::PlatformState(ps) => ps,
            ComObject::HostStateObservation(_) => {
                return Err(err("unsupported host-state observation for positional NVG"));
            }
        };
        if ps.platform_domain == PlatformDomain::Subsurface {
            return Err(err("subsurface domain has no qualified NVG mapping"));
        }
        let pos = ps
            .position
            .as_ref()
            .ok_or_else(|| err("missing position (x/y required by NVG point)"))?;

        let uri = xml_escape_attr(&ps.platform_id);

        // z（高度）は省略可: alt_m があるときだけ属性を出す（NVG は z 任意・default 0）。
        let z_attr = match pos.alt_m {
            Some(z) if z.is_finite() => format!(" z=\"{}\"", fmt_f64(z)),
            _ => String::new(),
        };

        // contract 可視ダウングレード: MARK を label（人間可読）と modifiers（機械可読）に運ぶ。
        let mark = &envelope.claim.mark;
        let label_text = format!("{} [{}]", ps.platform_id, mark_summary(mark));
        let label = xml_escape_attr(&label_text);
        // contractb-2: confidence の由来（BASIS）も modifiers に載せる（CoT remarks と対称・末尾 append）。
        let modifiers_text = format!(
            "MARK:{status};REASON:{reason};CONF:{conf};BASIS:{basis}",
            status = mark_status_label(mark.status),
            reason = if mark.reason_code.is_empty() {
                "none"
            } else {
                &mark.reason_code
            },
            conf = fmt_conf(envelope.claim.confidence),
            basis = fmt_basis(envelope.claim.confidence_basis.as_ref()),
        );
        let modifiers = xml_escape_attr(&modifiers_text);

        // TimeStamp 子要素（contract: observed_at 優先＋µs→ms 変換・CoT と同じ ISO 8601 UTC 変換を流用）。
        let when = iso8601_utc_from_epoch_ms(effective_time_ms(&ps.timestamps));

        // contract(b): symbol は platform_domain（Manifest 由来）から組み立てる
        // （旧実装は `app6b:SFGP------*****`＝friend+ground 無条件）。
        let symbol = app6b_symbol(ps.platform_domain);

        // x=経度・y=緯度（CoT と逆順！XSD LongLatType の (x=lon, y=lat) に厳密準拠）。
        let xml = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
             <nvg xmlns=\"{ns}\" version=\"{ver}\">\n  \
             <point uri=\"{uri}\" x=\"{x}\" y=\"{y}\"{z} symbol=\"{symbol}\" \
             label=\"{label}\" modifiers=\"{modifiers}\">\n    \
             <TimeStamp when=\"{when}\"/>\n  </point>\n\
             </nvg>\n",
            ns = NVG_NS,
            ver = NVG_VERSION,
            uri = uri,
            x = fmt_f64(pos.lon_deg), // x = 経度
            y = fmt_f64(pos.lat_deg), // y = 緯度
            z = z_attr,
            symbol = symbol,
            label = label,
            modifiers = modifiers,
            when = when,
        );

        // serializer policy construction gate: 送出前に **組み立てた bytes から独立に再導出**して policy を通す
        // （serializer の内部変数を信用しない＝teeth）。`reason_code`／`platform_id` に `& < > "` が
        // 入ると `xml_escape_attr` の作る `;` が `modifiers` の 4 フィールド境界を割るが、それは
        // ここで `PublishError` になり、`fanout::fan_out` の `SinkOutcome::Failed` として理由付きで
        // 会計される（no-silent-drop・per-publisher エラー隔離は不変）。CoT 側（serializer policy）と同じ構造で、
        // policy 実体は `nvg_policy` に 1 つだけ置く（写経して drift させない）。
        nvg_policy::check_nvg_document_bytes(xml.as_bytes())
            .map_err(|violation| err(&format!("nvg-policy:{violation}")))?;

        Ok(xml.into_bytes())
    }
}

#[cfg(test)]
mod cot {
    //! CoT serializer の単体（決定論・golden）。

    use super::{CotPublisher, NorthboundPublisher, StaleTtlConfig};
    use musubi_types::{
        Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformDomain, PlatformState,
        Position, Timestamps,
    };

    fn envelope_with(lat: f64, lon: f64, alt: Option<f64>, received_at: i64) -> EvidenceEnvelope {
        EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: "musubi-usv-001".to_string(),
                position: Some(Position {
                    lat_deg: lat,
                    lon_deg: lon,
                    alt_m: alt,
                }),
                mode: None,
                timestamps: Timestamps {
                    observed_at: None,
                    received_at,
                    time_confidence: 0.2,
                },
                // Domain-agnostic by default; domain-mapping tests (`*_domain_*`) mutate
                // `ps.platform_domain` after construction (contract(b)).
                platform_domain: PlatformDomain::Unknown,
            }),
            claim: Claim {
                claim_id: "c-1".to_string(),
                confidence: 0.6,
                mark: Mark {
                    status: MarkStatus::Ok,
                    reason_code: "normalized".to_string(),
                    provenance: vec![],
                },
                confidence_basis: None,
            },
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }
    }

    #[test]
    fn dialect_is_cot() {
        assert_eq!(CotPublisher::new().dialect(), "cot");
    }

    #[test]
    fn emits_uid_latlon_hae_and_sentinels() {
        let env = envelope_with(35.681236, 139.767125, Some(10.5), 0);
        let bytes = CotPublisher::new().publish(&env).expect("publishes");
        let xml = String::from_utf8(bytes).expect("utf8");

        assert!(xml.contains("uid=\"musubi-usv-001\""), "{xml}");
        assert!(xml.contains("lat=\"35.681236\""), "{xml}");
        assert!(xml.contains("lon=\"139.767125\""), "{xml}");
        assert!(xml.contains("hae=\"10.5\""), "{xml}");
        assert!(xml.contains("ce=\"9999999.0\""), "{xml}");
        assert!(xml.contains("le=\"9999999.0\""), "{xml}");
        assert!(xml.contains("version=\"2.0\""), "{xml}");
        // contract(b): `envelope_with` defaults to `PlatformDomain::Unknown` (domain not
        // declared) → type omits the battle-dimension segment entirely (`"a-u"`), and never
        // silently resolves to a specific domain (the old unconditional `a-f-G` bug).
        assert!(xml.contains("type=\"a-u\""), "{xml}");
        assert!(xml.contains("how=\"m-g\""), "{xml}");
        // received_at=0 → epoch → 1970-01-01T00:00:00.000Z、stale=+15s（既定 TTL・回帰なし）。
        assert!(xml.contains("time=\"1970-01-01T00:00:00.000Z\""), "{xml}");
        assert!(xml.contains("stale=\"1970-01-01T00:00:15.000Z\""), "{xml}");
    }

    #[test]
    fn air_domain_platform_renders_air_type_not_ground() {
        // contract(b) — the audit's exact defect: every platform (including UAVs) was drawn
        // with `type="a-f-G"` (friend, Ground). A UAV declared as `PlatformDomain::Air` must
        // render with the Air battle dimension, and must NEVER render as Ground.
        let mut env = envelope_with(35.681236, 139.767125, Some(10.5), 0);
        let ComObject::PlatformState(ps) = &mut env.observation else {
            panic!("expected PlatformState")
        };
        ps.platform_domain = PlatformDomain::Air;
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");

        assert!(xml.contains("type=\"a-u-A\""), "{xml}");
        assert!(
            !xml.contains("type=\"a-u-G\"") && !xml.contains("type=\"a-f-G\""),
            "a UAV (Air domain) must not be rendered as a Ground symbol: {xml}"
        );
    }

    #[test]
    fn surface_and_ground_domains_map_to_distinct_cot_types() {
        // Regression: Surface (USV) and Ground (UGV) domains must not collapse into the
        // same battle dimension, and neither collapses to the old unconditional Ground.
        let mut usv = envelope_with(35.0, 139.0, Some(0.0), 0);
        let ComObject::PlatformState(usv_ps) = &mut usv.observation else {
            panic!("expected PlatformState")
        };
        usv_ps.platform_domain = PlatformDomain::Surface;
        let usv_xml =
            String::from_utf8(CotPublisher::new().publish(&usv).expect("publishes")).expect("utf8");
        assert!(usv_xml.contains("type=\"a-u-S\""), "{usv_xml}");

        let mut ugv = envelope_with(35.0, 139.0, Some(0.0), 0);
        let ComObject::PlatformState(ugv_ps) = &mut ugv.observation else {
            panic!("expected PlatformState")
        };
        ugv_ps.platform_domain = PlatformDomain::Ground;
        let ugv_xml =
            String::from_utf8(CotPublisher::new().publish(&ugv).expect("publishes")).expect("utf8");
        assert!(ugv_xml.contains("type=\"a-u-G\""), "{ugv_xml}");
    }

    #[test]
    fn affiliation_is_unknown_not_friend() {
        // The audit flagged an internal contradiction: the module doc said "affiliation is
        // downstream consumer's call" while the code unconditionally asserted `f` (friend) itself.
        // The `u` (Unknown) segment must appear regardless of domain; `f` must not.
        let env = envelope_with(35.0, 139.0, Some(0.0), 0);
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("type=\"a-u\""), "{xml}");
        assert!(!xml.contains("type=\"a-f"), "{xml}");
    }

    // === contract: per-type stale TTL config + late-delivery vocabulary ===

    #[test]
    fn default_ttl_matches_prior_hardcoded_15s() {
        // Regression: the new `StaleTtlConfig` default must reproduce the old bare
        // `STALE_TTL_SECS = 15` constant exactly (no silent behavior change for existing
        // callers that only use `CotPublisher::new()`).
        let env = envelope_with(35.0, 139.0, Some(0.0), 0);
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("stale=\"1970-01-01T00:00:15.000Z\""), "{xml}");
    }

    #[test]
    fn custom_ttl_via_with_ttl_changes_stale_offset() {
        // contract: TTL is no longer a bare constant — callers can declare a grill-traced
        // value per COM type instead of the S0 default.
        let env = envelope_with(35.0, 139.0, Some(0.0), 0);
        let publisher = CotPublisher::with_ttl(StaleTtlConfig::new(60));
        let xml = String::from_utf8(publisher.publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("stale=\"1970-01-01T00:01:00.000Z\""), "{xml}");
        assert!(!xml.contains("stale=\"1970-01-01T00:00:15.000Z\""), "{xml}");
    }

    #[test]
    fn is_late_delivery_detects_backlog_drained_past_ttl() {
        // contract late-but-valid detection: an envelope observed at t=0 with a 15s TTL is
        // late if delivered (drained) after t=15s, and NOT late if delivered within TTL.
        assert!(super::is_late_delivery(0, 15, 15_001));
        assert!(!super::is_late_delivery(0, 15, 15_000));
        assert!(!super::is_late_delivery(0, 15, 1_000));
    }

    #[test]
    fn late_delivery_reason_code_is_visible_in_remarks_not_silently_dropped() {
        // contract no-silent-stale: once a caller (e.g. store-and-forward drain) marks a
        // backlog envelope with the late-delivery vocabulary token, northbound must carry
        // it through to the CoT <remarks> the same way any other reason_code is carried
        // (this is the existing MARK visibility pipe — the token itself is not lost).
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
        env.claim.mark.reason_code = super::LATE_DELIVERY_REASON_CODE.to_string();
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(
            xml.contains(&format!("MARK:OK:{}", super::LATE_DELIVERY_REASON_CODE)),
            "{xml}"
        );
    }

    #[test]
    fn missing_alt_falls_back_to_sentinel_hae() {
        let env = envelope_with(35.0, 139.0, None, 0);
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("hae=\"9999999.0\""), "{xml}");
    }

    #[test]
    fn iso8601_known_epoch() {
        // 2026-06-23T08:30:00.000Z = 1782… を逆算で検証（既知 epoch）。
        // 1782203400 s（= 2026-06-23T08:30:00Z）。
        let env = envelope_with(0.0, 0.0, Some(0.0), 1_782_203_400_000);
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("time=\"2026-06-23T08:30:00.000Z\""), "{xml}");
    }

    #[test]
    fn observed_at_micros_takes_priority_over_received_at_and_converts_units() {
        // contract: observed_at（µs）があれば received_at（ms）でなくそちらを優先し、
        // µs→ms 変換（/1000）してから ISO 8601 化する（桁ズレ回帰の防止）。
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 999_999_000);
        let ComObject::PlatformState(ps) = &mut env.observation else {
            panic!("expected PlatformState")
        };
        // observed_at = 1_782_203_400_000_000 µs = 2026-06-23T08:30:00.000Z（received_at とは無関係）。
        ps.timestamps.observed_at = Some(1_782_203_400_000_000);
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("time=\"2026-06-23T08:30:00.000Z\""), "{xml}");
        assert!(xml.contains("start=\"2026-06-23T08:30:00.000Z\""), "{xml}");
        assert!(xml.contains("stale=\"2026-06-23T08:30:15.000Z\""), "{xml}");
    }

    #[test]
    fn missing_observed_at_falls_back_to_received_at() {
        // observed_at=None（S0 MAVLink の典型）なら received_at（ms）をそのまま使う（回帰なし）。
        let env = envelope_with(35.0, 139.0, Some(0.0), 1_782_203_400_000);
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("time=\"2026-06-23T08:30:00.000Z\""), "{xml}");
    }

    #[test]
    fn uid_is_xml_escaped() {
        let mut env = envelope_with(0.0, 0.0, Some(0.0), 0);
        // ComObject は S0 では単一 variant＝irrefutable（後続 increment で variant 増えたら if let 化）。
        let ComObject::PlatformState(ps) = &mut env.observation else {
            panic!("expected PlatformState")
        };
        ps.platform_id = "a&b<c>\"".to_string();
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("uid=\"a&amp;b&lt;c&gt;&quot;\""), "{xml}");
    }

    #[test]
    fn near_zero_coords_use_decimal_not_scientific() {
        // 赤道/本初子午線近傍（|x|<1e-4）でも CoT 座標は十進固定小数で出す
        // （Debug {:?} だと "5e-5" の指数形になり TAK/ATAK が弾く＝回帰防止）。
        let env = envelope_with(0.000_05, 0.000_001, Some(0.0), 0);
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        // 十進固定小数（指数 'e' を含まない）。
        assert!(xml.contains("lat=\"0.00005\""), "{xml}");
        assert!(xml.contains("lon=\"0.000001\""), "{xml}");
        // round-trip（COM→northbound→decode・fail-on-loss＝contract EARS）。
        assert!((attr_f64(&xml, "lat") - 0.000_05).abs() <= 1e-6, "{xml}");
        assert!((attr_f64(&xml, "lon") - 0.000_001).abs() <= 1e-6, "{xml}");
    }

    /// テスト用: CoT XML から数値属性を抜く（最小・round-trip 検証用）。
    fn attr_f64(xml: &str, key: &str) -> f64 {
        let pat = format!("{key}=\"");
        let start = xml.find(&pat).expect("attr present") + pat.len();
        let rest = &xml[start..];
        let end = rest.find('"').expect("attr close");
        rest[..end].parse::<f64>().expect("attr parses as f64")
    }

    #[test]
    fn cot_carries_mark_in_remarks_visible_downgrade() {
        // S3 contract 可視ダウングレード: DEGRADED の MARK が CoT <remarks> に出る（黙って消えない）。
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
        env.claim.mark = Mark {
            status: MarkStatus::Degraded,
            reason_code: "time-suspect".to_string(),
            provenance: vec!["source:uav-01".to_string()],
        };
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("<remarks>"), "{xml}");
        assert!(xml.contains("MARK:DEGRADED:time-suspect"), "{xml}");
        assert!(xml.contains("CONF:0.60"), "{xml}");
    }

    #[test]
    fn compound_reason_code_does_not_break_top_level_field_split() {
        // contract 回帰: 複合 DEGRADED（core::derive_mark が "|" 連結した reason_code）でも、
        // remarks のトップレベル区切り ";" で split すると MARK/CONF/PROV/BASIS の 4 フィールドに
        // 綺麗に分解できる（reason_code が独自に ";" を持ち込んで境界と衝突しない）。
        // contractb-2: BASIS トークンを末尾 append したため 4 フィールド（旧 3＝MARK/CONF/PROV）。
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
        env.claim.mark = Mark {
            status: MarkStatus::Degraded,
            reason_code: "time-suspect|field-missing:hdg".to_string(),
            provenance: vec!["source:uav-01".to_string()],
        };
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        let remarks_start = xml.find("<remarks>").expect("remarks present") + "<remarks>".len();
        let remarks_end = xml.find("</remarks>").expect("remarks close");
        let remarks = &xml[remarks_start..remarks_end];
        let fields: Vec<&str> = remarks.split(';').collect();
        assert_eq!(
            fields.len(),
            4,
            "top-level split by ';' must yield exactly 4 fields (MARK/CONF/PROV/BASIS): {remarks:?}"
        );
        assert!(fields[0].starts_with("MARK:DEGRADED:time-suspect|field-missing:hdg"));
        assert!(fields[1].starts_with("CONF:"));
        assert!(fields[2].starts_with("PROV:"));
        assert!(fields[3].starts_with("BASIS:"));
    }

    /// contractb-2: confidence の由来（BASIS）が CoT `<remarks>` に載る（全 variant）。
    /// `None`→`unspecified`（honest＝measured と誤認させない）／`AdapterAssigned`→`adapter-assigned`。
    #[test]
    fn confidence_basis_is_projected_to_cot_remarks() {
        for (basis, expect) in [
            (None, "BASIS:unspecified"),
            (
                Some(musubi_types::ConfidenceBasis::AdapterAssigned),
                "BASIS:adapter-assigned",
            ),
        ] {
            let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
            env.claim.confidence_basis = basis;
            let xml = String::from_utf8(CotPublisher::new().publish(&env).expect("publishes"))
                .expect("utf8");
            assert!(
                xml.contains(expect),
                "expected {expect:?} in remarks: {xml}"
            );
        }
    }

    /// contractb-2 開示要件: `Measured` は「有無（measured）」だけを北向きに出し、`calibration_id` の
    /// 中身（較正台帳 ID＝内部/Kaname 参照）を wire に**漏らさない**。
    #[test]
    fn measured_basis_never_leaks_calibration_id_to_cot() {
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
        env.claim.confidence_basis = Some(musubi_types::ConfidenceBasis::Measured {
            calibration_id: Some("internal-cal-registry-xyz-2026".to_string()),
        });
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(
            xml.contains("BASIS:measured"),
            "measured basis must render: {xml}"
        );
        assert!(
            !xml.contains("internal-cal-registry-xyz-2026"),
            "calibration_id must NOT leak to the north-bound wire (contractb-2): {xml}"
        );
    }

    #[test]
    fn subsurface_has_no_unqualified_northbound_symbol() {
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
        let ComObject::PlatformState(ps) = &mut env.observation else {
            panic!("platform fixture required");
        };
        ps.platform_domain = PlatformDomain::Subsurface;
        for publisher in [
            &CotPublisher::new() as &dyn NorthboundPublisher,
            &super::NvgPublisher::new() as &dyn NorthboundPublisher,
        ] {
            let error = publisher
                .publish(&env)
                .expect_err("unqualified mapping must refuse");
            assert!(error.reason.contains("subsurface domain has no qualified"));
        }
    }

    // === serializer policy: 送出前 semantic allow-list（cot_policy）との接続 ===

    /// `DECLARED_COT_TYPES` が `cot_type()` の像と **過不足なく一致**すること（両方向）。
    ///
    /// `PlatformDomain` に variant が増えたら下の exhaustive match が compile error になり、
    /// allow-list の更新を強制する（const literal と serializer の drift を型で止める）。
    #[test]
    fn declared_types_cover_every_platform_domain_and_nothing_else() {
        const ALL: [PlatformDomain; 4] = [
            PlatformDomain::Air,
            PlatformDomain::Surface,
            PlatformDomain::Ground,
            PlatformDomain::Unknown,
        ];
        fn expected(domain: PlatformDomain) -> &'static str {
            match domain {
                PlatformDomain::Air => "a-u-A",
                PlatformDomain::Surface => "a-u-S",
                PlatformDomain::Ground => "a-u-G",
                PlatformDomain::Unknown => "a-u",
                PlatformDomain::Subsurface => {
                    unreachable!("unqualified export is tested separately")
                }
            }
        }

        let produced: Vec<String> = ALL.iter().map(|d| super::cot_type(*d)).collect();
        for (domain, got) in ALL.iter().zip(&produced) {
            assert_eq!(got, expected(*domain), "cot_type drifted for {domain:?}");
            assert!(
                crate::cot_policy::DECLARED_COT_TYPES.contains(&got.as_str()),
                "cot_type({domain:?}) = {got} is not in the declared allow-list"
            );
        }
        for declared in crate::cot_policy::DECLARED_COT_TYPES {
            assert!(
                produced.iter().any(|p| p == declared),
                "allow-list carries {declared} which no PlatformDomain produces"
            );
        }
    }

    /// 正例の網: 全 domain × 全 MARK status × 全 `ConfidenceBasis` が policy を通る
    /// （policy が product の正当な出力を誤って殺していないこと）。
    #[test]
    fn every_domain_and_mark_status_publishes_within_policy() {
        let statuses = [
            MarkStatus::Ok,
            MarkStatus::Degraded,
            MarkStatus::Invalid,
            MarkStatus::OrderUnknown,
            MarkStatus::Withheld,
            MarkStatus::StaleRevocation,
            MarkStatus::SelfAssertedTime,
            MarkStatus::BufferSaturated,
        ];
        let bases = [
            None,
            Some(musubi_types::ConfidenceBasis::AdapterAssigned),
            Some(musubi_types::ConfidenceBasis::Measured {
                calibration_id: Some("internal-cal".to_string()),
            }),
        ];
        for domain in [
            PlatformDomain::Air,
            PlatformDomain::Surface,
            PlatformDomain::Ground,
            PlatformDomain::Unknown,
        ] {
            for status in statuses {
                for basis in bases.clone() {
                    let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
                    let ComObject::PlatformState(ps) = &mut env.observation else {
                        panic!("expected PlatformState")
                    };
                    ps.platform_domain = domain;
                    env.claim.mark.status = status;
                    env.claim.confidence_basis = basis;
                    let bytes = CotPublisher::new()
                        .publish(&env)
                        .unwrap_or_else(|e| panic!("{domain:?}/{status:?} must publish: {e:?}"));
                    assert_eq!(
                        crate::cot_policy::check_cot_event_bytes(&bytes),
                        Ok(()),
                        "{domain:?}/{status:?} output must satisfy the egress policy"
                    );
                }
            }
        }
    }

    #[test]
    fn compound_reason_code_still_publishes_within_policy() {
        // contract の正当な形（`|` 連結）は殺さない。
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
        env.claim.mark.reason_code = "time-suspect|field-missing:hdg".to_string();
        let bytes = CotPublisher::new().publish(&env).expect("publishes");
        assert_eq!(crate::cot_policy::check_cot_event_bytes(&bytes), Ok(()));
    }

    #[test]
    fn semicolon_in_reason_code_is_refused_before_emit() {
        // 従来は remarks の 4 フィールド境界が黙って壊れた wire がそのまま出ていた。
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
        env.claim.mark.reason_code = "a;b".to_string();
        let error = CotPublisher::new()
            .publish(&env)
            .expect_err("a ';' in reason_code must be refused before emit");
        assert_eq!(error.dialect, "cot");
        assert!(
            error.reason.contains("cot-policy:remarks-field-count:5"),
            "{error:?}"
        );
    }

    #[test]
    fn ampersand_in_provenance_is_refused_before_emit() {
        // serializer policy で閉じる穴: `&` の escape が生む `&amp;` の `;` がフィールド境界を割る。
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0);
        env.claim.mark.provenance = vec!["source:a&b".to_string()];
        let error = CotPublisher::new()
            .publish(&env)
            .expect_err("an escaped entity in remarks must be refused before emit");
        assert!(error.reason.contains("undeclared-entity"), "{error:?}");
        // 拒否理由自体は wire 文法を壊さない（sanitize_reason）。
        assert!(!error.reason.contains(';'), "{error:?}");
    }

    #[test]
    fn affiliation_is_unknown_not_friend_at_policy_layer() {
        // contract(b) 回帰 test（serializer policy 完了条件）: serializer が `a-f-*` を出しても
        // policy が送出前に止める＝affiliation 非断定は 2 重に固定される。
        let env = envelope_with(35.0, 139.0, Some(0.0), 0);
        let xml =
            String::from_utf8(CotPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        let friendly = xml.replace("type=\"a-u\"", "type=\"a-f-G\"");
        assert!(
            crate::cot_policy::check_cot_event_bytes(friendly.as_bytes()).is_err(),
            "an affiliation-asserting type must never reach the wire: {friendly}"
        );
    }
}

#[cfg(test)]
mod nvg {
    //! NVG 2.0.2 serializer の単体（決定論・golden・x=lon/y=lat round-trip）。

    use super::{NorthboundPublisher, NvgPublisher};
    use musubi_types::{
        Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformDomain, PlatformState,
        Position, Timestamps,
    };

    fn envelope_with(
        lat: f64,
        lon: f64,
        alt: Option<f64>,
        received_at: i64,
        status: MarkStatus,
        reason: &str,
    ) -> EvidenceEnvelope {
        EvidenceEnvelope {
            observation: ComObject::PlatformState(PlatformState {
                platform_id: "musubi-usv-001".to_string(),
                position: Some(Position {
                    lat_deg: lat,
                    lon_deg: lon,
                    alt_m: alt,
                }),
                mode: None,
                timestamps: Timestamps {
                    observed_at: None,
                    received_at,
                    time_confidence: 0.2,
                },
                // Domain-agnostic by default; domain-mapping tests mutate
                // `ps.platform_domain` after construction (contract(b)).
                platform_domain: PlatformDomain::Unknown,
            }),
            claim: Claim {
                claim_id: "c-1".to_string(),
                confidence: 0.6,
                mark: Mark {
                    status,
                    reason_code: reason.to_string(),
                    provenance: vec!["source:musubi-usv-001".to_string()],
                },
                confidence_basis: None,
            },
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        }
    }

    /// テスト用: NVG XML から数値属性を抜く（round-trip 検証用・最小）。
    fn attr_f64(xml: &str, key: &str) -> f64 {
        let pat = format!("{key}=\"");
        let start = xml.find(&pat).expect("attr present") + pat.len();
        let rest = &xml[start..];
        let end = rest.find('"').expect("attr close");
        rest[..end].parse::<f64>().expect("attr parses as f64")
    }

    #[test]
    fn dialect_is_nvg() {
        assert_eq!(NvgPublisher::new().dialect(), "nvg");
    }

    #[test]
    fn emits_nvg_root_and_required_point_attrs() {
        let env = envelope_with(
            35.681236,
            139.767125,
            Some(10.5),
            0,
            MarkStatus::Ok,
            "normalized",
        );
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");

        // ルート <nvg> ＋ namespace ＋ version="2.0.0"。
        assert!(
            xml.contains("xmlns=\"https://tide.act.nato.int/schemas/2012/10/nvg\""),
            "{xml}"
        );
        assert!(xml.contains("version=\"2.0.0\""), "{xml}");
        // 必須属性 uri / x / y（NVG XSD で required）。
        assert!(xml.contains("uri=\"musubi-usv-001\""), "{xml}");
        // **x=経度・y=緯度（CoT と逆順）** — これが NVG の最も間違えやすい点。
        assert!(
            xml.contains("x=\"139.767125\""),
            "x must be longitude: {xml}"
        );
        assert!(xml.contains("y=\"35.681236\""), "y must be latitude: {xml}");
        assert!(xml.contains("z=\"10.5\""), "{xml}");
        // contract(b): `envelope_with` defaults to `PlatformDomain::Unknown` → affiliation `U`
        // (not the old unconditional `F`=friend) and battle dimension `-` (not `G`=ground).
        assert!(xml.contains("symbol=\"app6b:SU-P------*****\""), "{xml}");
        assert!(
            xml.contains("<TimeStamp when=\"1970-01-01T00:00:00.000Z\"/>"),
            "{xml}"
        );
    }

    #[test]
    fn air_domain_platform_does_not_render_as_ground_symbol_in_nvg() {
        // contract(b) — same defect as CoT, mirrored in NVG: a UAV (Air domain) must render
        // with the Air battle dimension and must never render as Ground.
        let mut env = envelope_with(
            35.681236,
            139.767125,
            Some(10.5),
            0,
            MarkStatus::Ok,
            "normalized",
        );
        let ComObject::PlatformState(ps) = &mut env.observation else {
            panic!("expected PlatformState")
        };
        ps.platform_domain = PlatformDomain::Air;
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");

        assert!(xml.contains("symbol=\"app6b:SUAP------*****\""), "{xml}");
        assert!(
            !xml.contains("symbol=\"app6b:SFGP------*****\"") && !xml.contains("SUGP------"),
            "a UAV (Air domain) must not be rendered as a Ground symbol: {xml}"
        );
    }

    #[test]
    fn x_is_lon_y_is_lat_round_trip() {
        // test ⑤: NVG golden（lat/lon round-trip）。x↔lon・y↔lat を許容誤差内で戻す。
        let env = envelope_with(
            35.4215,
            139.6423,
            Some(1.4),
            0,
            MarkStatus::Ok,
            "normalized",
        );
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        let ComObject::PlatformState(ps) = &env.observation else {
            panic!("expected PlatformState")
        };
        let pos = ps.position.as_ref().unwrap();
        // x=lon・y=lat（逆に取ると fail＝座標順序の回帰防止）。
        assert!((attr_f64(&xml, "x") - pos.lon_deg).abs() <= 1e-6, "{xml}");
        assert!((attr_f64(&xml, "y") - pos.lat_deg).abs() <= 1e-6, "{xml}");
    }

    #[test]
    fn missing_alt_omits_z_attribute() {
        // NVG の z は省略可: alt_m=None なら z 属性ごと出さない（CoT の hae sentinel と対照的）。
        let env = envelope_with(35.0, 139.0, None, 0, MarkStatus::Ok, "normalized");
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(
            !xml.contains(" z=\""),
            "z must be omitted when alt absent: {xml}"
        );
    }

    #[test]
    fn near_zero_coords_use_decimal_not_scientific() {
        // 赤道/本初子午線近傍でも十進固定小数（fmt_f64 再利用＝指数 'e' を出さない）。
        let env = envelope_with(
            0.000_05,
            0.000_001,
            Some(0.0),
            0,
            MarkStatus::Ok,
            "normalized",
        );
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("y=\"0.00005\""), "{xml}");
        assert!(xml.contains("x=\"0.000001\""), "{xml}");
    }

    #[test]
    fn observed_at_micros_takes_priority_over_received_at_in_nvg() {
        // contract: NVG の TimeStamp@when も observed_at（µs→ms 変換）優先で received_at を無視する。
        let mut env = envelope_with(
            35.0,
            139.0,
            Some(0.0),
            999_999_000,
            MarkStatus::Ok,
            "normalized",
        );
        let ComObject::PlatformState(ps) = &mut env.observation else {
            panic!("expected PlatformState")
        };
        ps.timestamps.observed_at = Some(1_782_203_400_000_000);
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(
            xml.contains("<TimeStamp when=\"2026-06-23T08:30:00.000Z\"/>"),
            "{xml}"
        );
    }

    #[test]
    fn degraded_mark_visible_in_label_and_modifiers() {
        // contract 可視ダウングレード: DEGRADED が label（人間可読）と modifiers（機械可読）に出る。
        let env = envelope_with(35.0, 139.0, Some(0.0), 0, MarkStatus::Degraded, "no-klv");
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(
            xml.contains("label=\"musubi-usv-001 [DEGRADED:no-klv]\""),
            "{xml}"
        );
        assert!(
            xml.contains("MARK:DEGRADED;REASON:no-klv;CONF:0.60"),
            "{xml}"
        );
    }

    #[test]
    fn uri_and_label_are_xml_escaped() {
        let mut env = envelope_with(0.0, 0.0, Some(0.0), 0, MarkStatus::Ok, "normalized");
        let ComObject::PlatformState(ps) = &mut env.observation else {
            panic!("expected PlatformState")
        };
        ps.platform_id = "a&b<c>\"".to_string();
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(xml.contains("uri=\"a&amp;b&lt;c&gt;&quot;\""), "{xml}");
    }

    /// contractb-2: confidence の由来（BASIS）が NVG `modifiers` に載る（CoT remarks と対称・全 variant）。
    #[test]
    fn confidence_basis_is_projected_to_nvg_modifiers() {
        for (basis, expect) in [
            (None, "BASIS:unspecified"),
            (
                Some(musubi_types::ConfidenceBasis::AdapterAssigned),
                "BASIS:adapter-assigned",
            ),
        ] {
            let mut env = envelope_with(35.0, 139.0, Some(0.0), 0, MarkStatus::Ok, "normalized");
            env.claim.confidence_basis = basis;
            let xml = String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes"))
                .expect("utf8");
            assert!(
                xml.contains(expect),
                "expected {expect:?} in modifiers: {xml}"
            );
        }
    }

    /// contractb-2 開示要件: NVG も `Measured` の有無だけを出し calibration_id を漏らさない。
    #[test]
    fn measured_basis_never_leaks_calibration_id_to_nvg() {
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0, MarkStatus::Ok, "normalized");
        env.claim.confidence_basis = Some(musubi_types::ConfidenceBasis::Measured {
            calibration_id: Some("internal-cal-registry-xyz-2026".to_string()),
        });
        let xml =
            String::from_utf8(NvgPublisher::new().publish(&env).expect("publishes")).expect("utf8");
        assert!(
            xml.contains("BASIS:measured"),
            "measured basis must render: {xml}"
        );
        assert!(
            !xml.contains("internal-cal-registry-xyz-2026"),
            "calibration_id must NOT leak to the north-bound wire (contractb-2): {xml}"
        );
    }

    // === serializer policy: 送出前 semantic allow-list（nvg_policy）との接続 ===

    /// `DECLARED_NVG_SYMBOLS` が `app6b_symbol()` の像と **過不足なく一致**すること（両方向）。
    ///
    /// `PlatformDomain` に variant が増えたら下の exhaustive match が compile error になり、
    /// allow-list の更新を強制する（const literal と serializer の drift を型で止める）。
    #[test]
    fn nvg_symbol_allow_list_matches_serializer() {
        const ALL: [PlatformDomain; 4] = [
            PlatformDomain::Air,
            PlatformDomain::Surface,
            PlatformDomain::Ground,
            PlatformDomain::Unknown,
        ];
        fn expected(domain: PlatformDomain) -> &'static str {
            match domain {
                PlatformDomain::Air => "app6b:SUAP------*****",
                PlatformDomain::Surface => "app6b:SUSP------*****",
                PlatformDomain::Ground => "app6b:SUGP------*****",
                PlatformDomain::Unknown => "app6b:SU-P------*****",
                PlatformDomain::Subsurface => {
                    unreachable!("unqualified export is tested separately")
                }
            }
        }

        let produced: Vec<String> = ALL.iter().map(|d| super::app6b_symbol(*d)).collect();
        for (domain, got) in ALL.iter().zip(&produced) {
            assert_eq!(
                got,
                expected(*domain),
                "app6b_symbol drifted for {domain:?}"
            );
            assert!(
                crate::nvg_policy::DECLARED_NVG_SYMBOLS.contains(&got.as_str()),
                "app6b_symbol({domain:?}) = {got} is not in the declared allow-list"
            );
        }
        for declared in crate::nvg_policy::DECLARED_NVG_SYMBOLS {
            assert!(
                produced.iter().any(|p| p == declared),
                "allow-list carries {declared} which no PlatformDomain produces"
            );
        }
    }

    /// 正例の網: 全 domain × 全 MARK status × 全 `ConfidenceBasis` が policy を通る
    /// （policy が product の正当な出力を誤って殺していないこと＝serializer policy の完了条件）。
    #[test]
    fn every_domain_and_mark_status_publishes_within_nvg_policy() {
        let statuses = [
            MarkStatus::Ok,
            MarkStatus::Degraded,
            MarkStatus::Invalid,
            MarkStatus::OrderUnknown,
            MarkStatus::Withheld,
            MarkStatus::StaleRevocation,
            MarkStatus::SelfAssertedTime,
            MarkStatus::BufferSaturated,
        ];
        let bases = [
            None,
            Some(musubi_types::ConfidenceBasis::AdapterAssigned),
            Some(musubi_types::ConfidenceBasis::Measured {
                calibration_id: Some("internal-cal".to_string()),
            }),
        ];
        for domain in [
            PlatformDomain::Air,
            PlatformDomain::Surface,
            PlatformDomain::Ground,
            PlatformDomain::Unknown,
        ] {
            for status in statuses {
                for basis in bases.clone() {
                    let mut env = envelope_with(35.0, 139.0, Some(0.0), 0, status, "normalized");
                    let ComObject::PlatformState(ps) = &mut env.observation else {
                        panic!("expected PlatformState")
                    };
                    ps.platform_domain = domain;
                    env.claim.confidence_basis = basis;
                    let bytes = NvgPublisher::new()
                        .publish(&env)
                        .unwrap_or_else(|e| panic!("{domain:?}/{status:?} must publish: {e:?}"));
                    assert_eq!(
                        crate::nvg_policy::check_nvg_document_bytes(&bytes),
                        Ok(()),
                        "{domain:?}/{status:?} output must satisfy the egress policy"
                    );
                }
            }
        }
    }

    /// 空 `reason_code`（`REASON:none`）も policy を通る（`mark_summary` の分岐両方を覆う）。
    #[test]
    fn empty_reason_code_still_publishes_within_nvg_policy() {
        let env = envelope_with(35.0, 139.0, Some(0.0), 0, MarkStatus::Ok, "");
        let bytes = NvgPublisher::new().publish(&env).expect("publishes");
        assert_eq!(crate::nvg_policy::check_nvg_document_bytes(&bytes), Ok(()));
        let xml = String::from_utf8(bytes).expect("utf8");
        assert!(xml.contains("REASON:none"), "{xml}");
    }

    #[test]
    fn compound_reason_code_still_publishes_within_nvg_policy() {
        // contract の正当な形（`|` 連結・実データの absence token）は殺さない。
        let env = envelope_with(
            35.0,
            139.0,
            Some(0.0),
            0,
            MarkStatus::Degraded,
            "absence-expected-cadence|absent-since:2026-08-31T09:00:59.000Z",
        );
        let bytes = NvgPublisher::new().publish(&env).expect("publishes");
        assert_eq!(crate::nvg_policy::check_nvg_document_bytes(&bytes), Ok(()));
    }

    /// **serializer policy の核（負例）**: `reason_code` の `& < > "` は escape されて `modifiers` の
    /// 4 フィールド境界を割るので、送出前に `PublishError` として止まる。
    #[test]
    fn escapable_char_in_reason_code_is_refused_before_emit() {
        for reason in ["parse&fail", "a<b", "a>b", "a\"b"] {
            let env = envelope_with(35.0, 139.0, Some(0.0), 0, MarkStatus::Degraded, reason);
            let error = NvgPublisher::new()
                .publish(&env)
                .expect_err("an escaped entity in modifiers must be refused before emit");
            assert!(
                error
                    .reason
                    .starts_with("nvg-policy:escaped-field-content:modifiers:"),
                "{reason} → {error:?}"
            );
            // 拒否理由自体が `;` を持たない（serializer policy `sanitize_reason` と同じ穴を再生産しない）。
            assert!(!error.reason.contains(';'), "{error:?}");
        }
    }

    /// 負例: 生の `;` を含む `reason_code` はフィールド数で止まる。
    #[test]
    fn raw_semicolon_in_reason_code_is_refused_before_emit() {
        let env = envelope_with(35.0, 139.0, Some(0.0), 0, MarkStatus::Degraded, "a;b");
        let error = NvgPublisher::new().publish(&env).expect_err("refused");
        assert_eq!(
            error.reason,
            "nvg-policy:modifiers-field-count:5:expected=4"
        );
    }

    /// 負例: `platform_id` の危険符号点は `uri`／`label` で止まる（charset を縛る）。
    #[test]
    fn hostile_platform_id_is_refused_before_emit() {
        let mut env = envelope_with(35.0, 139.0, Some(0.0), 0, MarkStatus::Ok, "normalized");
        let ComObject::PlatformState(ps) = &mut env.observation else {
            panic!("expected PlatformState")
        };
        ps.platform_id = "usv\u{202E}01".to_string();
        let error = NvgPublisher::new().publish(&env).expect_err("refused");
        assert_eq!(error.reason, "nvg-policy:disallowed-codepoint:uri:U+202E");
    }
}

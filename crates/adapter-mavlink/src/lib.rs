//! MAVLink2 read-only tap（initial 最初のアダプタ）。
//!
//! 受動傍受のみ（mavlink-router SnifferSysID＝Musubi プロセス外の傍受ノードに置く）。
//! このアダプタは heartbeat も command も **出さない**（X-NOWRITE・read-only-by-wiring）。
//!
//! initial（S0）で **GLOBAL_POSITION_INT (msgid=33)** だけを hand-rolled・zero-dep で
//! デコードし、COM `PlatformState` へ正規化する（contract の最小 oracle 1 本）。実デコードの
//! `mavlink` crate 全面採用・他 msgid・WASI Preview 2 component 化（deny-by-default
//! capability）は後続 increment（最初の cargo build を確実に通すため Day-1 は zero-dep）。

use musubi_core::{
    Normalizer, ObservationQuality, QualityNote, RawObservation, derive_mark, seal_digest,
};
use musubi_types::{
    Claim, ComObject, EvidenceEnvelope, Mark, MarkStatus, PlatformState, Position, Timestamps,
};

/// `hdg`（cdeg）の「不明」sentinel（`UINT16_MAX`）。COM PlatformState に heading 枠は無いが、
/// 不明であることは品質シグナル（DEGRADED 寄与）として MARK に surface する（黙って消さない）。
const HDG_UNKNOWN: u16 = u16::MAX;

/// 受動傍受 source の契約。観測を **取り出す**だけで、書き込み口を持たない。
///
/// `poll` は注入・送信をしない（read-only）。実装は別 netns の tap に置き、
/// arm を出す側（GCS）は tap から到達不能にする（配線で物理担保）。
pub trait ReadOnlyTap {
    /// この tap の source 識別子。
    fn source_id(&self) -> &str;

    /// 次の生観測を取り出す（無ければ `None`）。注入・送信はしない。
    fn poll(&mut self) -> Option<RawObservation>;
}

// === MAVLink2 wire 定数（mavlink.io serialization spec から確定） ===

/// MAVLink2 の固定スタートバイト（STX）。
const MAVLINK2_MAGIC: u8 = 0xFD;
/// MAVLink1 のスタートバイト（S0 は MAVLink2 専用＝reject する）。
const MAVLINK1_MAGIC: u8 = 0xFE;
/// MAVLink2 フレームヘッダ長（magic..msgid の 10 バイト固定）。
const HEADER_LEN: usize = 10;
/// `GLOBAL_POSITION_INT` の msgid。
const MSGID_GLOBAL_POSITION_INT: u32 = 33;
/// `GLOBAL_POSITION_INT` ペイロードの**最大**長（全フィールドで 28 バイト）。
///
/// ⚠️ contract: MAVLink2 の **trailing-zero truncation は per-frame 動的な必須仕様**（送信側が
/// 末尾のゼロバイトを切り詰め、受信側がゼロ復元する）。実 autopilot（ArduPilot/PX4 の標準
/// C lib `_mav_trim_payload`・pymavlink）は hdg が真北付近（±2.55°）等で末尾バイトが 0 に
/// なると `len < 28` の**正規フレーム**を出す。よって受理条件は `1 <= len <= 28` であり、
/// 「本型は truncation 対象外＝28 固定」という以前の per-type 静的判断は誤読（正規フレーム
/// 全棄却）だった。
const GLOBAL_POSITION_INT_LEN: usize = 28;
/// `incompat_flags` bit0（署名あり）。S0 ではこのビットが立つと署名 13 バイトが付く前提。
const IFLAG_SIGNED: u8 = 0x01;
/// MAVLink2 署名部のバイト長（`incompat_flags` bit0 が立つとき末尾に付く）。
const SIGNATURE_LEN: usize = 13;
/// MAVLink2 CRC-16/MCRF4XX のバイト長（payload の直後・署名の直前に**常に**付く固定長）。
///
/// contract: `payload_slice`/`decode_global_position_int_payload` の `need` 計算はこの
/// CRC 分を数えない（S0 は CRC 値そのものを検証しない＝別 TODO）。quarantine の
/// [`reject_frame_length_ambiguity`] はフレーム**境界**（受領バイト数）だけを見るため、
/// CRC の**存在**（2 バイト）は数えなければならない。
const CRC_LEN: usize = 2;
/// MAVLink2 の**プロトコル上の**単一フレーム最大バイト数
/// （header(10) + payload(255=`u8::MAX`) + CRC(2) + signature(13) = 280）。
///
/// contract quarantine: `RawObservation.payload` は 1 フレーム分の生バイト列という契約
/// （南向き read-only tap の 1 recv = 1 フレーム）。この上限を超える入力は、単一フレームの
/// 皮を被った何か別物（複数フレームの結合・でたらめな注入等）である可能性が高く、プロトコル
/// 定義上ありえないサイズなので他の全チェックより先に reject する（DoS 耐性の広い網）。
const MAVLINK2_MAX_FRAME_BYTES: usize = HEADER_LEN + 255 + CRC_LEN + SIGNATURE_LEN;
// hdg=65535（UINT16_MAX）は「不明」。COM PlatformState に heading 枠が無い S0 では
// hdg を COM に載せないため sentinel 定数は後続 increment（heading 追加時）に置く。

/// `GLOBAL_POSITION_INT`（msgid=33）の wire フィールド（全 little-endian）。
///
/// COM `PlatformState` へ写すのは位置（lat/lon/alt）のみ（narrow spine＝contract）。
/// heading/velocity は将来 increment 用に保持するが S0 では COM に載せない。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GlobalPositionInt {
    /// システム起動からの経過 ms（**絶対時刻ではない**＝SYSTEM_TIME とペアで offset 確立要）。
    pub time_boot_ms: u32,
    /// 緯度 degE7（deg = value / 1e7）。
    pub lat: i32,
    /// 経度 degE7（deg = value / 1e7）。
    pub lon: i32,
    /// MSL 高度 mm（m = value / 1000）。
    pub alt: i32,
    /// home 相対高度 mm（m = value / 1000）。
    pub relative_alt: i32,
    /// 対地速度 X（北正）cm/s。
    pub vx: i16,
    /// 対地速度 Y（東正）cm/s。
    pub vy: i16,
    /// 対地速度 Z（NED 下正）cm/s。
    pub vz: i16,
    /// 機首方位 cdeg（0..=36000・不明=`UINT16_MAX`）。
    pub hdg: u16,
}

/// MAVLink2 フレームのデコード失敗（§5 #4: 失敗を黙って落とさず材料化する）。
///
/// ⚠️ これは **malformed（構造不当）** の語彙である。「構造妥当だが S0 未マップの msgid」は
/// デコード失敗ではなく [`UnmappedFrame`]（別 outcome・contract）として扱う。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecodeError {
    /// magic が MAVLink2(0xFD) でない（MAVLink1 0xFE 含む・S0 は MAVLink2 専用）。
    NotMavlink2 { magic: u8 },
    /// フレームがヘッダ＋宣言ペイロード長（＋署名）に満たない（truncated/corrupt）。
    TooShort { have: usize, need: usize },
    /// msgid が想定（GLOBAL_POSITION_INT=33）でない（GPI 専用 decode 経路のみが返す）。
    UnexpectedMsgId { msgid: u32 },
    /// ヘッダの `len` が GLOBAL_POSITION_INT の受理範囲 `1..=28` の外
    /// （contract: trailing-zero truncation により 28 未満は**正規**。0 と 29 以上のみ不正）。
    BadPayloadLen { len: u8 },
}

/// MAVLink2 固定ヘッダ（10 バイト）のデコード結果。
///
/// contract: `sysid`/`compid`（byte 5-6）は**送信元 identity**。本 crate が前提に置く
/// mavlink-router SnifferSysID 傍受（モジュール doc）では 1 本の tap（source_id）に複数機体の
/// フレームが多重化されるため、これを読まず `platform_id = source_id` とすると複数機体が単一
/// uid に折り畳まれ track aliasing（TAK 上で 1 トラックが機体間をテレポート）を起こす。
/// `normalize` は `platform_id = "{source_id}/sys{sysid}"` へ合成して demux する。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mavlink2Header {
    /// 宣言ペイロード長（trailing-zero truncation により型の最大長より短くなりうる・contract）。
    pub len: u8,
    /// 非互換フラグ（bit0=署名 13 バイト付き）。
    pub incompat_flags: u8,
    /// 互換フラグ。
    pub compat_flags: u8,
    /// フレーム連番。
    pub seq: u8,
    /// 送信システム ID（機体 identity・contract demux の鍵）。
    pub sysid: u8,
    /// 送信コンポーネント ID（同一機体内の autopilot/カメラ等の区別・来歴に保持）。
    pub compid: u8,
    /// メッセージ ID（24bit little-endian）。
    pub msgid: u32,
}

/// MAVLink2 固定ヘッダ（10 バイト）を **read-only** にデコードする。
///
/// # Errors
/// magic が MAVLink2(0xFD) でない／ヘッダ 10 バイトに満たないとき [`DecodeError`]。
pub fn parse_mavlink2_header(frame: &[u8]) -> Result<Mavlink2Header, DecodeError> {
    // (1) magic（MAVLink2 専用ガード・MAVLink1 0xFE は明示 reject）。
    let magic = *frame.first().ok_or(DecodeError::TooShort {
        have: frame.len(),
        need: HEADER_LEN,
    })?;
    if magic != MAVLINK2_MAGIC {
        // MAVLINK1_MAGIC もここで弾く（混在環境は後続 increment で判別）。
        let _ = MAVLINK1_MAGIC;
        return Err(DecodeError::NotMavlink2 { magic });
    }

    // (2) ヘッダ 10 バイトが無ければ truncated。
    if frame.len() < HEADER_LEN {
        return Err(DecodeError::TooShort {
            have: frame.len(),
            need: HEADER_LEN,
        });
    }

    Ok(Mavlink2Header {
        len: frame[1],
        incompat_flags: frame[2],
        compat_flags: frame[3],
        seq: frame[4],
        sysid: frame[5],
        compid: frame[6],
        // msgid（byte[7..10]）24bit little-endian。
        msgid: u32::from(frame[7]) | (u32::from(frame[8]) << 8) | (u32::from(frame[9]) << 16),
    })
}

/// フレームが「ヘッダ＋宣言ペイロード（＋署名）」を完備しているかを msgid に依らず検査し、
/// ペイロード部（宣言 `len` バイト）を返す（**構造妥当性**の共通検査・contract）。
fn payload_slice<'a>(frame: &'a [u8], header: &Mavlink2Header) -> Result<&'a [u8], DecodeError> {
    let signed = (header.incompat_flags & IFLAG_SIGNED) != 0;
    let payload_len = usize::from(header.len);
    let need = HEADER_LEN + payload_len + if signed { SIGNATURE_LEN } else { 0 };
    if frame.len() < need {
        return Err(DecodeError::TooShort {
            have: frame.len(),
            need,
        });
    }
    Ok(&frame[HEADER_LEN..HEADER_LEN + payload_len])
}

/// MAVLink2 フレーム（バイト列）から `GLOBAL_POSITION_INT` を **read-only** にデコードする。
///
/// 仕様（mavlink.io）に厳密準拠: magic=0xFD・10 バイトヘッダ・msgid 24bit LE・ペイロード
/// little-endian。**trailing-zero truncation（`1 <= len <= 28`）を受理**し、不足分は受信側で
/// ゼロ復元する（contract・per-frame 動的な必須仕様）。
///
/// # Errors
/// magic 不一致 / truncated / msgid 不一致 / `len` が `1..=28` の外のとき [`DecodeError`]。
///
/// # TODO（後続 increment）
/// CRC-16/MCRF4XX（CRC_EXTRA=104）検証・MAVLink1(0xFE) 併存判別・他 msgid のマップ。
/// S0 は frame 構造の正しさと field 抽出の正しさ（contract）に限定。
pub fn decode_global_position_int(frame: &[u8]) -> Result<GlobalPositionInt, DecodeError> {
    let header = parse_mavlink2_header(frame)?;
    if header.msgid != MSGID_GLOBAL_POSITION_INT {
        return Err(DecodeError::UnexpectedMsgId {
            msgid: header.msgid,
        });
    }
    decode_global_position_int_payload(frame, &header)
}

/// ヘッダ確定済みフレームから `GLOBAL_POSITION_INT` ペイロードを decode する（truncation 復元込み）。
fn decode_global_position_int_payload(
    frame: &[u8],
    header: &Mavlink2Header,
) -> Result<GlobalPositionInt, DecodeError> {
    // contract: trailing-zero truncation を受理（1 <= len <= 28）。len=0（truncation でも最低
    // 1 バイトは残る）と len>28（本型の最大超過）だけが不正。
    if header.len == 0 || usize::from(header.len) > GLOBAL_POSITION_INT_LEN {
        return Err(DecodeError::BadPayloadLen { len: header.len });
    }
    let truncated = payload_slice(frame, header)?;

    // 受信側ゼロ復元（mavlink.io serialization: 切り詰められた末尾バイトは 0 として扱う）。
    let mut p = [0u8; GLOBAL_POSITION_INT_LEN];
    p[..truncated.len()].copy_from_slice(truncated);

    let le_u32 = |o: usize| u32::from_le_bytes([p[o], p[o + 1], p[o + 2], p[o + 3]]);
    let le_i32 = |o: usize| i32::from_le_bytes([p[o], p[o + 1], p[o + 2], p[o + 3]]);
    let le_i16 = |o: usize| i16::from_le_bytes([p[o], p[o + 1]]);
    let le_u16 = |o: usize| u16::from_le_bytes([p[o], p[o + 1]]);

    Ok(GlobalPositionInt {
        time_boot_ms: le_u32(0),
        lat: le_i32(4),
        lon: le_i32(8),
        alt: le_i32(12),
        relative_alt: le_i32(16),
        vx: le_i16(20),
        vy: le_i16(22),
        vz: le_i16(24),
        hdg: le_u16(26),
    })
}

/// contract: S0 が Claim に載せる confidence の既定値。**上流 MAVLink wire に confidence
/// フィールドは存在しない**＝これは adapter が割り当てた値（adapter-assigned）であって、上流から
/// propagate した実測確度ではない。O-8②（confidence を生成でなく運ぶ／誤った自信を新たに
/// 付けない）に従い、この値を使う Claim の provenance には必ず [`CONFIDENCE_ORIGIN_NOTE`] を
/// 積み、下流 C2 が実測値と区別できるようにする（定数 confidence を黙って発明しない）。
const ADAPTER_ASSIGNED_CONFIDENCE: f32 = 0.6;

/// contract: adapter 既定 confidence の由来 marker（controlled-vocabulary token・`;`/`|` 不使用）。
///
/// [`ADAPTER_ASSIGNED_CONFIDENCE`] を Claim に載せる正規化経路は、この token を provenance
/// （→ 北向き CoT `<remarks>` の `PROV:` / NVG `modifiers`）へ**必ず**積む（恒等 property:
/// wire 由来でない confidence には origin marker が伴う＝fabrication 判別可能）。
///
/// contract フォローアップ: adapter-usv-json / adapter-template / docs/offline-components.md と
/// 表記を `"confidence:adapter-assigned"` に統一（従来の `"confidence-origin:adapter-assigned"`
/// は本 adapter だけの異表記だった）。
const CONFIDENCE_ORIGIN_NOTE: &str = "confidence:adapter-assigned";

/// raw bytes → 小文字 hex（zero-dep・provenance 経由の無損失 raw 保持用・contract）。
fn hex_lower(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        // String への write! は失敗しない（fmt::Error は Vec backing では発生しない）。
        let _ = write!(s, "{b:02x}");
    }
    s
}

/// contract: **構造妥当**な MAVLink2 フレームだが S0 未マップの msgid（HEARTBEAT 等）。
///
/// malformed（truncated/corrupt → INVALID `"map-unknown"`）とは**別 outcome**: フレームは
/// 壊れておらず正規トラフィックである。features A1-e「未知メッセージ drop 禁止＝raw 保持＋MARK」
/// に従い、生フレーム全体を無損失保持する（後続 increment／新 FW メッセージを後から再解釈する
/// 経路を消さない）。
#[derive(Debug, Clone, PartialEq)]
pub struct UnmappedFrame {
    /// tap の source 識別子。
    pub source_id: String,
    /// 未マップの msgid（24bit）。
    pub msgid: u32,
    /// 送信システム ID（contract: 未マップでも identity は保持する）。
    pub sysid: u8,
    /// 送信コンポーネント ID。
    pub compid: u8,
    /// 受領フレーム全体（header＋payload＋CRC/署名含む・無損失 raw 保持）。
    pub raw: Vec<u8>,
    /// MARK（`status=Degraded`・`reason_code="unmapped-msgid"`）。
    ///
    /// INVALID にしない（contract）: 正規未マップ流量を INVALID に畳むと decode error 率が
    /// 恒常的に膨らみ、contract の rot シグナル（error 率上昇＝FW 変化の兆候）を埋もれさせる。
    /// ここでの DEGRADED の含意は「受領・保持したが S0 では COM へ写せない＝raw のままでのみ
    /// 使える（減衰して使え）」。provenance の `raw-frame-hex:` で raw が retrievable。
    pub mark: Mark,
}

/// contract: mavlink adapter の正規化 outcome（mapped / unmapped を**別 outcome** に分離）。
///
/// malformed は第 3 の経路として `Err(NormalizeError)`（INVALID `"map-unknown"`）で表す。
#[derive(Debug, Clone, PartialEq)]
pub enum MavlinkOutcome {
    /// マップ済み msgid（S0: GLOBAL_POSITION_INT）→ COM へ正規化・seal 済み envelope。
    Normalized(EvidenceEnvelope),
    /// 構造妥当だが S0 未マップの msgid（raw 全 bytes 保持＋MARK・drop しない）。
    Unmapped(UnmappedFrame),
}

/// contract quarantine: [`musubi_core::quarantine::QuarantineReject`] を、他の decode-failure
/// 経路（`malformed`）と**同型だが別語彙**の [`musubi_core::NormalizeError`] へ写す。
///
/// reason_code は 3 アダプタ共通の [`musubi_core::quarantine::QUARANTINE_REASON_CODE`]
/// （`"quarantine-rejected"`）を**必ず**使う（アダプタごとに別語彙を発明しない＝contract の核）。
/// 具体的な拒否事由は `Display` 経由で provenance に残す（no-silent-drop）。
fn quarantine_error(
    raw: &RawObservation,
    reject: musubi_core::quarantine::QuarantineReject,
) -> musubi_core::NormalizeError {
    musubi_core::NormalizeError {
        source_id: raw.source_id.clone(),
        mark: Mark {
            status: MarkStatus::Invalid,
            reason_code: musubi_core::quarantine::QUARANTINE_REASON_CODE.to_string(),
            provenance: vec![
                format!("source:{}", raw.source_id),
                "adapter:mavlink/global_position_int".to_string(),
                format!("quarantine:{reject}"),
            ],
        },
    }
}

/// contract quarantine（**reject-on-ambiguity**）: 単一フレーム観測の受領バイト数が、ヘッダ自身の
/// 構造宣言（`len` ＋ 常設 CRC ＋ 署名フラグ）から求まる**期待バイト数**と一致するかを検査する。
///
/// 一致しなければフレーム境界が曖昧（extra な末尾バイトが別フレームの先頭の混入なのか、単なる
/// ゴミなのか、あるいは何かが欠けているのかを Musubi 側で判別できない）ということであり、
/// `payload_slice`/`decode_global_position_int_payload` の「少なくとも need バイトあるか」
/// （contract の trailing-zero truncation 受理はこちらが担う・不変）に**追加**して、常に厳密一致を
/// 要求する。既存の truncation 受理ロジックは一切変更しない（この関数は decode 成功後に呼ぶ
/// **追加**チェック）。
///
/// # Errors
/// バイト数が一致しなければ [`musubi_core::quarantine::QuarantineReject::FrameLengthMismatch`]。
fn reject_frame_length_ambiguity(
    total_len: usize,
    header: &Mavlink2Header,
) -> Result<(), musubi_core::quarantine::QuarantineReject> {
    let signed = (header.incompat_flags & IFLAG_SIGNED) != 0;
    let expected =
        HEADER_LEN + usize::from(header.len) + CRC_LEN + if signed { SIGNATURE_LEN } else { 0 };
    if total_len == expected {
        Ok(())
    } else {
        Err(
            musubi_core::quarantine::QuarantineReject::FrameLengthMismatch {
                expected_bytes: expected,
                actual_bytes: total_len,
            },
        )
    }
}

/// MAVLink2 → COM `PlatformState` 正規化器（`musubi_core::Normalizer` 実装）。
///
/// 決定論・read-only。raw バイト列を受け取り COM を内包した [`EvidenceEnvelope`] を返す。
/// 失敗は黙って落とさず [`musubi_core::NormalizeError`]（MARK 付き）にする（§5 #4）。
/// 3 outcome（mapped／unmapped／malformed）を無損失に扱う呼び出し側は
/// [`MavlinkNormalizer::normalize_frame`] を使う（contract）。
#[derive(Debug, Clone)]
pub struct MavlinkNormalizer {
    platform_domain: musubi_types::PlatformDomain,
}

impl Default for MavlinkNormalizer {
    fn default() -> Self {
        Self::new()
    }
}

impl MavlinkNormalizer {
    #[must_use]
    pub fn new() -> Self {
        Self::with_platform_domain(musubi_types::PlatformDomain::Air)
    }

    /// Declare the domain of this source from operator/manifest configuration.
    /// GPI wire data does not identify the vehicle domain. The declaration applies
    /// to all sysids on this tap; use separate configured taps for mixed domains.
    /// It is covered by the existing COM digest and grants no control authority.
    #[must_use]
    pub const fn with_platform_domain(platform_domain: musubi_types::PlatformDomain) -> Self {
        Self { platform_domain }
    }

    /// フレームを decode し、mapped / unmapped を**別 outcome**で返す（contract）。
    ///
    /// - マップ済み msgid（S0: GLOBAL_POSITION_INT）→ [`MavlinkOutcome::Normalized`]。
    /// - 構造妥当・未マップ msgid → [`MavlinkOutcome::Unmapped`]（raw 全 bytes 保持＋MARK）。
    /// - malformed（非 MAVLink2・truncated・len 不正）→ `Err`＝INVALID `"map-unknown"`。
    ///
    /// # Errors
    /// フレームが malformed（構造不当）のとき [`musubi_core::NormalizeError`]（INVALID MARK 付き・
    /// no-silent-drop）。
    pub fn normalize_frame(
        &self,
        raw: &RawObservation,
    ) -> Result<MavlinkOutcome, musubi_core::NormalizeError> {
        // contract quarantine boundary（ガードレール#2 の実装接地点＝the quarantine contract）:
        // 生バイト列を **他の何より先に** 大きさで検疫する（プロトコル上ありえないサイズは
        // 単一フレームの皮を被った別物とみなし、以降の一切の parse を試みない）。
        musubi_core::quarantine::quarantine_payload_size(&raw.payload, MAVLINK2_MAX_FRAME_BYTES)
            .map_err(|reject| quarantine_error(raw, reject))?;

        // malformed（構造不当）→ INVALID "map-unknown"（従来語彙を維持・contract で意味を
        // 「malformed 専用」に狭めた）。
        let malformed = |e: &DecodeError| musubi_core::NormalizeError {
            source_id: raw.source_id.clone(),
            mark: Mark {
                status: MarkStatus::Invalid,
                reason_code: "map-unknown".to_string(),
                provenance: vec![
                    format!("source:{}", raw.source_id),
                    format!("mavlink-decode-error:{e:?}"),
                ],
            },
        };

        let header = parse_mavlink2_header(&raw.payload).map_err(|e| malformed(&e))?;

        if header.msgid != MSGID_GLOBAL_POSITION_INT {
            // contract: 未マップ msgid。構造妥当性（宣言 len 分のペイロード完備）はここでも
            // 検査する — truncated な未知フレームは malformed（INVALID）のまま。
            payload_slice(&raw.payload, &header).map_err(|e| malformed(&e))?;
            // contract quarantine（reject-on-ambiguity）: 宣言 len と実受領バイト数が厳密一致するか
            // （CRC・署名込み）。未知 msgid でも raw を無損失保持する前に境界の曖昧さは弾く。
            reject_frame_length_ambiguity(raw.payload.len(), &header)
                .map_err(|reject| quarantine_error(raw, reject))?;
            return Ok(MavlinkOutcome::Unmapped(UnmappedFrame {
                source_id: raw.source_id.clone(),
                msgid: header.msgid,
                sysid: header.sysid,
                compid: header.compid,
                raw: raw.payload.clone(),
                mark: Mark {
                    status: MarkStatus::Degraded,
                    reason_code: "unmapped-msgid".to_string(),
                    provenance: vec![
                        format!("source:{}", raw.source_id),
                        "adapter:mavlink/global_position_int".to_string(),
                        format!("mavlink-ident:sys{}-comp{}", header.sysid, header.compid),
                        format!("unmapped-msgid:{}", header.msgid),
                        // 無損失 raw 保持（A1-e）: 狭い trait 経路（Result 2 値）からでも
                        // raw が retrievable（hex decode で復元可能）。
                        format!("raw-frame-hex:{}", hex_lower(&raw.payload)),
                    ],
                },
            }));
        }

        let msg =
            decode_global_position_int_payload(&raw.payload, &header).map_err(|e| malformed(&e))?;

        // contract quarantine（reject-on-ambiguity）: マップ済み msgid でも同じフレーム境界検査を
        // 課す（未マップ経路とチェックを揃える＝アダプタ全体で 1 つの quarantine 契約）。
        reject_frame_length_ambiguity(raw.payload.len(), &header)
            .map_err(|reject| quarantine_error(raw, reject))?;

        // 位置を COM へ（lat/lon degE7→deg・alt mm→m MSL）。
        // heading/velocity は narrow spine（contract）では PlatformState に枠が無いため S0 は載せない。
        let position = Position {
            lat_deg: deg_e7_to_deg(msg.lat),
            lon_deg: deg_e7_to_deg(msg.lon),
            alt_m: Some(mm_to_m(msg.alt)),
        };

        // time_boot_ms は **絶対時刻でない**（boot 相対）。SYSTEM_TIME offset 未確立の S0 では
        // observed_at を主張せず（None）、time_confidence を低く置く（§5 #4・contract）。
        // この低い confidence は core::derive_mark で DEGRADED("time-suspect") に写る（S1）。
        let time_confidence = 0.2_f32;
        let timestamps = Timestamps {
            observed_at: None,
            // contract: ingest 層（transport/tap）が刻んだ受領時刻をそのまま転記する
            // （以前は adapter が `0` を決め打ちしていた＝二重時刻が構造的に潰れる原因だった）。
            received_at: raw.received_at,
            time_confidence,
        };

        // contract: 共有テレメトリチャネル（mavlink-router SnifferSysID 傍受）の demux —
        // 機体 identity は sysid。platform_id は tap の source_id と sysid の合成
        // `{source_id}/sys{sysid}`（compid は provenance に保持: GPI は通常 autopilot compid=1。
        // 同一機体内の別コンポーネント発は identity を割らず来歴で区別する）。
        let platform_id = format!("{}/sys{}", raw.source_id, header.sysid);

        let platform = PlatformState {
            platform_id: platform_id.clone(),
            position: Some(position),
            mode: None,
            timestamps,
            // contract(b): domain comes from source configuration, not the GPI msgid.
            // new()/default() retain the original Air profile for compatibility.
            platform_domain: self.platform_domain,
        };

        // S1: MARK は core が品質シグナルから決定論で導出する（X-MARK・語彙を core に一元化）。
        // adapter は「観測の品質の事実」を notes に積むだけ（status の写像は core::derive_mark）。
        let mut notes = vec![
            // alt は MSL(AMSL)。CoT hae は本来 WGS-84 楕円体高(HAE)＝S0/S1 は未補正で載せるため
            // 来歴に明示 surface する（黙って HAE と偽らない・geoid 補正は後続 increment）。中立注釈。
            QualityNote::neutral("alt-ref:MSL-uncorrected"),
            // time_boot_ms が絶対時刻でないこと自体の来歴（時刻 DEGRADED の根拠を可視化）。
            QualityNote::neutral("time-boot-ms-not-absolute"),
            // contract: confidence は上流 wire に存在しない＝adapter 既定値であることを必ず明示
            // （O-8②「confidence を生成でなく運ぶ」・下流 C2 が実測値と区別できる）。中立注釈。
            QualityNote::neutral(CONFIDENCE_ORIGIN_NOTE),
            // contract: 送信元 identity の来歴（compid を含む・demux の監査トレース）。中立注釈。
            QualityNote::neutral(format!(
                "mavlink-ident:sys{}-comp{}",
                header.sysid, header.compid
            )),
        ];
        // contract: trailing-zero truncation を受信側でゼロ復元した事実の来歴（正規動作・中立注釈）。
        if usize::from(header.len) < GLOBAL_POSITION_INT_LEN {
            notes.push(QualityNote::neutral(format!(
                "payload-truncation-restored:len={}",
                header.len
            )));
        }
        // hdg=UINT16_MAX は「不明」。COM に heading 枠は無いが品質低下事由として DEGRADED 寄与。
        if msg.hdg == HDG_UNKNOWN {
            notes.push(QualityNote::degraded("hdg=unknown", "field-missing:hdg"));
        }
        let quality = ObservationQuality {
            source_id: raw.source_id.clone(),
            adapter_slug: "mavlink/global_position_int".to_string(),
            time_confidence,
            notes,
        };
        let mark = derive_mark(&quality);
        let claim = Claim {
            // contract: claim_id も demux 済み platform_id 起点（source_id 直はチャネル多重化で
            // 複数機体の claim_id が衝突する）。
            claim_id: format!("{platform_id}:global_position_int"),
            // contract: adapter-assigned 既定値（観測の主張であって確定でない・上流 wire に
            // confidence は存在しない＝provenance に CONFIDENCE_ORIGIN_NOTE を必ず積む）。
            confidence: ADAPTER_ASSIGNED_CONFIDENCE,
            mark,
            // contract: adapter が割り当てた既定値の由来を型でも明示（provenance の
            // CONFIDENCE_ORIGIN_NOTE と対・下流 C2 が実測値と区別できる）。
            confidence_basis: Some(musubi_types::ConfidenceBasis::AdapterAssigned),
        };

        // fan-out 手前の単一正本を確定（content digest を充填・contract base）。signature は None。
        // contract §3.5 予約フィールド（signer_id/signed_at/revocation_proof）は source preview 未設定＝None
        // （canonical_bytes 除外＝golden 不変・contract impl-deferred）。
        Ok(MavlinkOutcome::Normalized(seal_digest(EvidenceEnvelope {
            observation: ComObject::PlatformState(platform),
            claim,
            classification: None,
            signature: None,
            content_digest: None,
            signer_id: None,
            signed_at: None,
            revocation_proof: None,
            trust_annotations: None,
        })))
    }
}

/// `degE7`（int32・×1e7 度）を度に戻す（仕様の mapping ルール）。
fn deg_e7_to_deg(v: i32) -> f64 {
    f64::from(v) / 1e7
}

/// `mm`（int32）を m に戻す。
fn mm_to_m(v: i32) -> f64 {
    f64::from(v) / 1000.0
}

impl Normalizer for MavlinkNormalizer {
    fn normalize(
        &self,
        raw: &RawObservation,
    ) -> Result<EvidenceEnvelope, musubi_core::NormalizeError> {
        match self.normalize_frame(raw)? {
            MavlinkOutcome::Normalized(envelope) => Ok(envelope),
            // contract: 狭い trait（Result 2 値）では unmapped を Err で運ぶが、malformed とは
            // **畳まない**: status=DEGRADED・reason_code="unmapped-msgid"（INVALID "map-unknown"
            // と別語彙）・provenance の `raw-frame-hex:` で raw が retrievable（no-silent-drop）。
            // 3 outcome を無損失に扱いたい呼び出し側は `normalize_frame` を使う。
            MavlinkOutcome::Unmapped(unmapped) => {
                let UnmappedFrame {
                    source_id, mark, ..
                } = unmapped;
                Err(musubi_core::NormalizeError { source_id, mark })
            }
        }
    }
}

#[cfg(test)]
mod ec002 {
    //! contract 正規化の受入 oracle（Green）。
    //!
    //! 既知 GLOBAL_POSITION_INT バイト列 → COM `PlatformState` の field-level マッピングを
    //! golden で検証する（誤マップ=0・observed・許容誤差 1e-6）。MAVLINK SPEC の golden_example
    //! のフレームをそのまま入力に使う。

    use super::{
        DecodeError, MavlinkNormalizer, MavlinkOutcome, decode_global_position_int,
        parse_mavlink2_header,
    };
    use musubi_core::{Normalizer, RawObservation, verify_digest};
    use musubi_types::{ComObject, MarkStatus};

    /// MAVLINK SPEC golden_example の 40 バイトフレーム（署名なし・sysid=1/compid=1/seq=0）。
    /// lat=47.397742 / lon=8.545594 / alt=488 m MSL / hdg=270.00 / vx=100 / vz=-10。
    /// end-to-end test（同 crate `ec002_end_to_end`）からも参照するため `pub(crate)`。
    pub(crate) const GOLDEN_FRAME: [u8; 40] = [
        0xFD, 0x1C, 0x00, 0x00, 0x00, 0x01, 0x01, 0x21, 0x00, 0x00, // header(10)
        0x39, 0x30, 0x00, 0x00, // time_boot_ms = 12345
        0x4C, 0x52, 0x40, 0x1C, // lat = 473977420
        0x44, 0xF4, 0x17, 0x05, // lon = 85455940
        0x40, 0x72, 0x07, 0x00, // alt = 488000 mm
        0x50, 0xC3, 0x00, 0x00, // relative_alt = 50000 mm
        0x64, 0x00, // vx = 100
        0x00, 0x00, // vy = 0
        0xF6, 0xFF, // vz = -10
        0x78, 0x69, // hdg = 27000 cdeg
        0x21, 0xA3, // CRC (CRC-16/MCRF4XX) — S0 は未検証（後続 increment）
    ];

    #[test]
    fn decode_fields_bit_exact() {
        let msg = decode_global_position_int(&GOLDEN_FRAME).expect("golden frame decodes");
        assert_eq!(msg.time_boot_ms, 12345);
        assert_eq!(msg.lat, 473_977_420);
        assert_eq!(msg.lon, 85_455_940);
        assert_eq!(msg.alt, 488_000);
        assert_eq!(msg.relative_alt, 50_000);
        assert_eq!(msg.vx, 100);
        assert_eq!(msg.vy, 0);
        assert_eq!(msg.vz, -10);
        assert_eq!(msg.hdg, 27_000);
    }

    #[test]
    fn normalize_maps_position_within_tolerance() {
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: GOLDEN_FRAME.to_vec(),
            received_at: 0,
        };
        let env = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect("golden frame normalizes");

        let ComObject::PlatformState(ps) = &env.observation else {
            panic!("expected PlatformState")
        };
        let pos = ps.position.as_ref().expect("position present");

        // degE7 → deg / mm → m。許容誤差 1e-6（仕様宣言）。
        assert!(
            (pos.lat_deg - 47.397_742).abs() < 1e-6,
            "lat={}",
            pos.lat_deg
        );
        assert!(
            (pos.lon_deg - 8.545_594).abs() < 1e-6,
            "lon={}",
            pos.lon_deg
        );
        assert!(
            (pos.alt_m.expect("alt present") - 488.0).abs() < 1e-6,
            "alt={:?}",
            pos.alt_m
        );

        // contract: platform_id は source_id と sysid の合成（GOLDEN_FRAME は sysid=1）。
        assert_eq!(ps.platform_id, "uav-01/sys1");
        // time_boot_ms は絶対時刻でない → observed_at を主張しない・confidence 低。
        assert_eq!(ps.timestamps.observed_at, None);
        assert!(ps.timestamps.time_confidence <= 0.5);
        // S1: golden frame は **デコード成功だが時刻が boot 相対**（time_confidence=0.2 ≤ 閾値）
        // → core::derive_mark が DEGRADED("time-suspect") を付ける（S0 の素朴 OK から精緻化）。
        // no-silent-drop で reason_code は非空・provenance に source/adapter が積まれる（T-1）。
        assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
        assert!(env.claim.mark.reason_code.starts_with("time-suspect"));
        assert!(!env.claim.mark.reason_code.is_empty());
        assert!(
            env.claim.mark.provenance.len() >= 2,
            "provenance must carry at least source+adapter: {:?}",
            env.claim.mark.provenance
        );
        // S1: fan-out 手前で content_digest が充填される（contract base・tamper-evident）。
        assert!(env.content_digest.is_some(), "envelope must be sealed");
        // S1 は署名を実装しない（contract・contract TOE 外）＝None のまま（欠陥ではない）。
        assert!(env.signature.is_none());
    }

    #[test]
    fn mavlink1_frame_is_rejected() {
        // MAVLink1(0xFE) は S0 では reject（map-unknown を MARK して落とさない）。
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: vec![0xFE, 0x1C, 0x00, 0x00, 0x00, 0x01, 0x01, 0x21, 0x00, 0x00],
            received_at: 0,
        };
        let err = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect_err("mavlink1 must not normalize");
        assert_eq!(err.mark.status, MarkStatus::Invalid);
    }

    #[test]
    fn wrong_msgid_gpi_decode_path_reports_unexpected_msgid() {
        // GPI 専用 decode 経路（decode_global_position_int）は msgid 不一致を UnexpectedMsgId
        // で報告する（正規化層 contract の unmapped 分離とは別の低レベル契約）。
        let mut frame = GOLDEN_FRAME;
        frame[7] = 0x00; // msgid lo byte = 0（HEARTBEAT）
        assert!(matches!(
            decode_global_position_int(&frame),
            Err(DecodeError::UnexpectedMsgId { msgid: 0 })
        ));
    }

    // === contract: 未知 msgid（正規未マップ）と malformed の分離 ===

    #[test]
    fn unmapped_msgid_is_separate_outcome_with_raw_retained() {
        // contract: 構造妥当だが S0 未マップの msgid（HEARTBEAT=0）は malformed と**別 outcome**。
        // 以前は truncated/corrupt と同じ INVALID "map-unknown" に畳まれ raw も失われていた。
        let mut frame = GOLDEN_FRAME;
        frame[7] = 0x00; // msgid lo byte = 0（len=28 のまま＝ペイロード完備・構造妥当）
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: frame.to_vec(),
            received_at: 0,
        };
        let outcome = MavlinkNormalizer::new()
            .normalize_frame(&raw)
            .expect("well-formed unmapped frame must not be malformed");
        let MavlinkOutcome::Unmapped(uf) = outcome else {
            panic!("unmapped msgid must yield MavlinkOutcome::Unmapped, got {outcome:?}");
        };
        // raw 全 bytes が無損失保持される（A1-e「未知メッセージ drop 禁止＝raw 保持＋MARK」）。
        assert_eq!(uf.raw, frame.to_vec());
        assert_eq!(uf.msgid, 0);
        assert_eq!(uf.sysid, 1);
        assert_eq!(uf.compid, 1);
        // MARK は INVALID でなく DEGRADED（正規未マップ流量が contract rot シグナルを埋もれさせない）。
        assert_eq!(uf.mark.status, MarkStatus::Degraded);
        assert_eq!(uf.mark.reason_code, "unmapped-msgid");
    }

    #[test]
    fn unmapped_heartbeat_shaped_frame_is_unmapped_not_malformed() {
        // 実形状の HEARTBEAT（msgid=0・len=9）: header(10)+payload(9)+crc(2)。
        let mut frame = vec![
            0xFD, 0x09, 0x00, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00, // header
        ];
        frame.extend_from_slice(&[0u8; 9]); // HEARTBEAT payload（9 バイト）
        frame.extend_from_slice(&[0x00, 0x00]); // CRC placeholder
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: frame,
            received_at: 0,
        };
        let outcome = MavlinkNormalizer::new()
            .normalize_frame(&raw)
            .expect("spec-valid HEARTBEAT must not be treated as corrupt");
        assert!(
            matches!(outcome, MavlinkOutcome::Unmapped(_)),
            "HEARTBEAT must be Unmapped (was previously collapsed into BadPayloadLen/INVALID)"
        );
    }

    #[test]
    fn unmapped_via_trait_is_distinct_from_malformed_and_raw_retrievable() {
        // 狭い trait（Normalizer::normalize）経路でも unmapped ≠ malformed:
        // reason_code が分離され、provenance の raw-frame-hex から raw が復元可能。
        let mut unmapped_frame = GOLDEN_FRAME;
        unmapped_frame[7] = 0x00; // 構造妥当・未マップ msgid
        let unmapped_err = MavlinkNormalizer::new()
            .normalize(&RawObservation {
                source_id: "uav-01".to_string(),
                payload: unmapped_frame.to_vec(),
                received_at: 0,
            })
            .expect_err("unmapped cannot map to COM through the narrow trait");
        assert_eq!(unmapped_err.mark.status, MarkStatus::Degraded);
        assert_eq!(unmapped_err.mark.reason_code, "unmapped-msgid");
        let hex = unmapped_err
            .mark
            .provenance
            .iter()
            .find_map(|p| p.strip_prefix("raw-frame-hex:"))
            .expect("raw must be retrievable from provenance (A1-e)");
        assert_eq!(
            hex.len(),
            unmapped_frame.len() * 2,
            "lossless hex of the full frame"
        );
        assert!(hex.starts_with("fd1c"), "hex={hex}");

        // 対照: truncated な未知 msgid フレーム（len=9 宣言・payload 4 バイトのみ）は malformed。
        let corrupt = vec![
            0xFD, 0x09, 0x00, 0x00, 0x00, 0x01, 0x01, 0x00, 0x00, 0x00, 0xAA, 0xBB,
        ];
        let malformed_err = MavlinkNormalizer::new()
            .normalize(&RawObservation {
                source_id: "uav-01".to_string(),
                payload: corrupt,
                received_at: 0,
            })
            .expect_err("truncated frame must be malformed");
        assert_eq!(malformed_err.mark.status, MarkStatus::Invalid);
        assert_eq!(malformed_err.mark.reason_code, "map-unknown");
        // 語彙レベルで分離されている（同一 reason_code へ畳まない）。
        assert_ne!(
            unmapped_err.mark.reason_code,
            malformed_err.mark.reason_code
        );
    }

    // === contract: MAVLink2 trailing-zero truncation（受理＋ゼロ復元） ===

    #[test]
    fn truncated_north_heading_frame_is_accepted_and_zero_restored() {
        // contract golden: 実 autopilot（_mav_trim_payload）は hdg=0（真北）・vy/vz=0 のとき
        // 末尾ゼロを切り詰める。golden の payload で vz/hdg を 0 とすると最後の non-zero は
        // vx lo（payload offset 20 = 0x64）→ len=21 の正規 truncated フレームになる。
        let mut frame = GOLDEN_FRAME[..10].to_vec();
        frame[1] = 21; // len = 21（trailing-zero truncation）
        frame.extend_from_slice(&GOLDEN_FRAME[10..30]); // time..relative_alt（20 バイト）
        frame.push(0x64); // vx lo = 100（最後の non-zero バイト）
        frame.extend_from_slice(&[0x00, 0x00]); // CRC placeholder（S0 未検証）

        // decode: 不足分（vx hi・vy・vz・hdg）はゼロ復元される。
        let msg = decode_global_position_int(&frame).expect("truncated frame must decode");
        assert_eq!(msg.time_boot_ms, 12345);
        assert_eq!(msg.lat, 473_977_420);
        assert_eq!(msg.lon, 85_455_940);
        assert_eq!(msg.alt, 488_000);
        assert_eq!(msg.vx, 100);
        assert_eq!(msg.vy, 0);
        assert_eq!(msg.vz, 0);
        assert_eq!(msg.hdg, 0, "hdg=0（真北）はゼロ復元で正しく現れる");

        // normalize: INVALID にならず位置が COM へ載る。hdg=0 は「真北」であって「不明(65535)」
        // ではない → field-missing:hdg は付かない。truncation 復元の来歴が残る。
        let env = MavlinkNormalizer::new()
            .normalize(&RawObservation {
                source_id: "uav-01".to_string(),
                payload: frame,
                received_at: 0,
            })
            .expect("real-autopilot truncated frame must normalize (was rejected pre-contract)");
        assert!(
            !env.claim.mark.reason_code.contains("field-missing:hdg"),
            "hdg=0 is a valid north heading, not unknown: {}",
            env.claim.mark.reason_code
        );
        assert!(
            env.claim
                .mark
                .provenance
                .iter()
                .any(|p| p == "payload-truncation-restored:len=21"),
            "truncation restore must be surfaced in provenance: {:?}",
            env.claim.mark.provenance
        );
    }

    #[test]
    fn minimal_truncation_len1_is_accepted() {
        // truncation の極限（spec: 最低 1 バイトは残る）。全フィールドほぼ 0 の GPI。
        let mut frame = GOLDEN_FRAME[..10].to_vec();
        frame[1] = 1; // len = 1
        frame.push(0x39); // time_boot_ms lo
        frame.extend_from_slice(&[0x00, 0x00]); // CRC placeholder
        let msg = decode_global_position_int(&frame).expect("len=1 truncated frame decodes");
        assert_eq!(msg.time_boot_ms, 0x39);
        assert_eq!(msg.lat, 0);
        assert_eq!(msg.hdg, 0);
    }

    #[test]
    fn len_zero_and_oversize_len_are_malformed() {
        // len=0（truncation でも最低 1 バイト残る）と len>28（型最大超過）は BadPayloadLen。
        let mut zero = GOLDEN_FRAME[..10].to_vec();
        zero[1] = 0;
        zero.extend_from_slice(&[0x00, 0x00]);
        assert!(matches!(
            decode_global_position_int(&zero),
            Err(DecodeError::BadPayloadLen { len: 0 })
        ));

        let mut oversize = GOLDEN_FRAME.to_vec();
        oversize[1] = 29;
        oversize.push(0x00); // 宣言分のバイトはあっても型最大超過で reject
        assert!(matches!(
            decode_global_position_int(&oversize),
            Err(DecodeError::BadPayloadLen { len: 29 })
        ));

        // 正規化層では malformed（INVALID "map-unknown"）に落ちる。
        let err = MavlinkNormalizer::new()
            .normalize(&RawObservation {
                source_id: "uav-01".to_string(),
                payload: zero,
                received_at: 0,
            })
            .expect_err("len=0 GPI must be malformed");
        assert_eq!(err.mark.status, MarkStatus::Invalid);
        assert_eq!(err.mark.reason_code, "map-unknown");
    }

    // === contract: sysid/compid demux（共有チャネルの track aliasing 防止） ===

    #[test]
    fn header_parse_extracts_sysid_compid() {
        let h = parse_mavlink2_header(&GOLDEN_FRAME).expect("golden header parses");
        assert_eq!(h.sysid, 1);
        assert_eq!(h.compid, 1);
        assert_eq!(h.msgid, 33);
        assert_eq!(h.len, 28);
        assert_eq!(h.seq, 0);
    }

    #[test]
    fn multi_vehicle_shared_channel_demuxes_by_sysid() {
        // contract: 同一 tap（source_id 固定＝mavlink-router SnifferSysID 傍受の標準形）に
        // 2 機体（sysid=1/2）が多重化 → platform_id / claim_id が機体ごとに分離される
        // （以前は platform_id=source_id で単一 uid に折り畳まれ track aliasing）。
        let mut frame_sys2 = GOLDEN_FRAME;
        frame_sys2[5] = 0x02; // sysid = 2
        let n = MavlinkNormalizer::new();
        let e1 = n
            .normalize(&RawObservation {
                source_id: "gcs-tap".to_string(),
                payload: GOLDEN_FRAME.to_vec(),
                received_at: 0,
            })
            .expect("sys1 frame normalizes");
        let e2 = n
            .normalize(&RawObservation {
                source_id: "gcs-tap".to_string(),
                payload: frame_sys2.to_vec(),
                received_at: 0,
            })
            .expect("sys2 frame normalizes");

        let ComObject::PlatformState(p1) = &e1.observation else {
            panic!("expected PlatformState")
        };
        let ComObject::PlatformState(p2) = &e2.observation else {
            panic!("expected PlatformState")
        };
        assert_eq!(p1.platform_id, "gcs-tap/sys1");
        assert_eq!(p2.platform_id, "gcs-tap/sys2");
        assert_ne!(
            p1.platform_id, p2.platform_id,
            "two vehicles on one shared channel must not collapse into one uid"
        );
        assert_ne!(
            e1.claim.claim_id, e2.claim.claim_id,
            "claim_id must not collide across vehicles"
        );
        // compid は identity を割らず来歴（監査トレース）に保持される。
        assert!(
            e1.claim
                .mark
                .provenance
                .iter()
                .any(|p| p == "mavlink-ident:sys1-comp1"),
            "provenance must carry sender identity: {:?}",
            e1.claim.mark.provenance
        );
    }

    // === contract: confidence の由来明示（fabrication 判別可能性） ===

    #[test]
    fn adapter_assigned_confidence_carries_origin_marker() {
        // contract / O-8②: MAVLink wire に confidence フィールドは存在しない。Claim.confidence
        // は adapter 既定値なので、provenance に confidence:adapter-assigned が**必ず**
        // 載る（恒等 property: 上流由来でない confidence には origin marker が伴う＝下流 C2 が
        // 実測値と区別できる・fabrication=0 oracle の最小形）。adapter-usv-json / adapter-template
        // と同じ marker 文字列に統一（contract フォローアップ）。
        let env = MavlinkNormalizer::new()
            .normalize(&RawObservation {
                source_id: "uav-01".to_string(),
                payload: GOLDEN_FRAME.to_vec(),
                received_at: 0,
            })
            .expect("golden frame normalizes");
        assert!(
            (env.claim.confidence - 0.6).abs() < 1e-6,
            "S0 default stays 0.6 (adapter-assigned): {}",
            env.claim.confidence
        );
        assert!(
            env.claim
                .mark
                .provenance
                .iter()
                .any(|p| p == "confidence:adapter-assigned"),
            "adapter-assigned confidence must be explicitly marked in provenance: {:?}",
            env.claim.mark.provenance
        );
    }

    // === S1 追加 oracle（X-MARK status 別 / contract / no-silent-drop） ===

    #[test]
    fn truncated_frame_is_invalid_not_dropped() {
        // T-2 入力C: ヘッダ 5 バイトのみ（truncated）→ INVALID・MARK 付き（落とさない）。
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: vec![0xFD, 0x1C, 0x00, 0x00, 0x00],
            received_at: 0,
        };
        let err = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect_err("truncated frame must not normalize");
        assert_eq!(err.mark.status, MarkStatus::Invalid);
        assert_eq!(err.mark.reason_code, "map-unknown");
        assert!(!err.mark.reason_code.is_empty());
    }

    /// hdg=UINT16_MAX（不明）の golden frame は DEGRADED("...field-missing:hdg") を付ける。
    /// 位置はあるが heading が不明＝品質低下（「減衰して使え」）。INVALID にはしない。
    #[test]
    fn unknown_heading_degrades_not_invalid() {
        let mut frame = GOLDEN_FRAME;
        // hdg は payload offset 26（frame offset 10+26=36..38）= UINT16_MAX(0xFFFF)。
        frame[36] = 0xFF;
        frame[37] = 0xFF;
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: frame.to_vec(),
            received_at: 0,
        };
        let env = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect("decodes (hdg unknown is degrade, not decode-error)");
        assert_eq!(env.claim.mark.status, MarkStatus::Degraded);
        assert!(
            env.claim.mark.reason_code.contains("field-missing:hdg"),
            "reason={}",
            env.claim.mark.reason_code
        );
    }

    #[test]
    fn ec004_one_bit_meta_tamper_is_detected_end_to_end() {
        // T-3 / contract base: normalize→seal 済み envelope の meta を 1-bit 変えて再検証 →
        // INVALID(meta-tamper-detected)。tamper-evident（偶発腐敗の検出）であって改竄不可ではない。
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: GOLDEN_FRAME.to_vec(),
            received_at: 0,
        };
        let sealed = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect("normalizes");
        // 受領直後は整合（OK）。
        assert_eq!(verify_digest(&sealed).status, MarkStatus::Ok);

        // meta を 1-bit 改竄（digest は古いまま）。
        let mut tampered = sealed;
        let ComObject::PlatformState(ps) = &mut tampered.observation else {
            panic!("expected PlatformState")
        };
        let pos = ps.position.as_mut().expect("position present");
        pos.lat_deg = f64::from_bits(pos.lat_deg.to_bits() ^ 1);

        let mark = verify_digest(&tampered);
        assert_eq!(mark.status, MarkStatus::Invalid);
        assert_eq!(mark.reason_code, "meta-tamper-detected");
    }

    #[test]
    fn no_silent_drop_for_all_inputs() {
        // T-5: Ok/Err どちらでも必ず MARK が付き reason_code が非空（黙って捨てない）。
        let mut unmapped = GOLDEN_FRAME.to_vec();
        unmapped[7] = 0x00; // 構造妥当・未マップ msgid（contract 経路も no-silent-drop）
        let inputs: Vec<Vec<u8>> = vec![
            GOLDEN_FRAME.to_vec(), // 正常（DEGRADED 想定）
            vec![0xFE, 0x1C, 0x00, 0x00, 0x00, 0x01, 0x01, 0x21, 0x00, 0x00], // MAVLink1
            vec![0xFD, 0x1C, 0x00, 0x00, 0x00], // truncated
            vec![],                // 空
            unmapped,              // 正規未マップ（DEGRADED "unmapped-msgid"）
        ];
        for payload in inputs {
            let raw = RawObservation {
                source_id: "uav-01".to_string(),
                payload,
                received_at: 0,
            };
            let result = MavlinkNormalizer::new().normalize(&raw);
            let reason_nonempty = result.as_ref().map_or_else(
                |e| !e.mark.reason_code.is_empty(),
                |ok| !ok.claim.mark.reason_code.is_empty(),
            );
            assert!(
                reason_nonempty,
                "every outcome must carry a non-empty MARK reason_code"
            );
        }
    }

    #[test]
    fn s1_never_fires_reserved_mark_values() {
        // T-6: 全フレームセットを通して MARK は OK/DEGRADED/INVALID のみ（予約 5 値は出ない＝
        // ORDER_UNKNOWN/WITHHELD/STALE_REVOCATION/SELF_ASSERTED_TIME〔contract §3.3〕/
        // BUFFER_SATURATED〔contract〕）。
        let golden = RawObservation {
            source_id: "uav-01".to_string(),
            payload: GOLDEN_FRAME.to_vec(),
            received_at: 0,
        };
        let bad = RawObservation {
            source_id: "uav-01".to_string(),
            payload: vec![0xFE, 0x00],
            received_at: 0,
        };
        let mut statuses = Vec::new();
        match MavlinkNormalizer::new().normalize(&golden) {
            Ok(env) => statuses.push(env.claim.mark.status),
            Err(e) => statuses.push(e.mark.status),
        }
        match MavlinkNormalizer::new().normalize(&bad) {
            Ok(env) => statuses.push(env.claim.mark.status),
            Err(e) => statuses.push(e.mark.status),
        }
        for s in statuses {
            assert!(
                matches!(
                    s,
                    MarkStatus::Ok | MarkStatus::Degraded | MarkStatus::Invalid
                ),
                "S1 must not emit reserved MarkStatus, got {s:?}"
            );
        }
    }
}

#[cfg(test)]
mod aud2_11_quarantine {
    //! contract: 非信頼入力 quarantine harness の接地点（ガードレール#2 実装接地＝
    //! the quarantine contract）。既存の decode/truncation（contract/15）契約を変えず、
    //! **追加**の reject-on-ambiguity / payload-size 検疫だけを検証する。

    use super::ec002::GOLDEN_FRAME;
    use super::{MavlinkNormalizer, MavlinkOutcome};
    use musubi_core::quarantine::QUARANTINE_REASON_CODE;
    use musubi_core::{Normalizer, RawObservation};
    use musubi_types::MarkStatus;

    #[test]
    fn golden_frame_exact_length_still_normalizes() {
        // 回帰: quarantine 追加後も GOLDEN_FRAME（header+payload+CRC ぴったり）は無傷で通る。
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: GOLDEN_FRAME.to_vec(),
            received_at: 0,
        };
        assert!(MavlinkNormalizer::new().normalize(&raw).is_ok());
    }

    #[test]
    fn oversize_payload_is_quarantined_before_any_parsing() {
        // MAVLink2 のプロトコル上ありえない大きさ（280 バイト超）は decode を試みる前に reject。
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: vec![0xFDu8; 281],
            received_at: 0,
        };
        let err = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect_err("oversize payload must be rejected");
        assert_eq!(err.mark.status, MarkStatus::Invalid);
        assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
        assert!(
            err.mark
                .provenance
                .iter()
                .any(|p| p.starts_with("quarantine:payload-oversize:")),
            "{:?}",
            err.mark.provenance
        );
    }

    #[test]
    fn trailing_extra_byte_after_a_well_formed_mapped_frame_is_ambiguous() {
        // GOLDEN_FRAME（header+payload+CRC ぴったり）の直後に 1 バイト付け足すと、
        // その 1 バイトが「別フレームの先頭が紛れ込んだのか／ゴミなのか」曖昧になる →
        // reject-on-ambiguity（黙って先頭 40 バイトだけ解釈しない）。
        let mut frame = GOLDEN_FRAME.to_vec();
        frame.push(0xAA);
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: frame,
            received_at: 0,
        };
        let err = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect_err("trailing extra byte must be rejected as ambiguous");
        assert_eq!(err.mark.status, MarkStatus::Invalid);
        assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
        assert!(
            err.mark
                .provenance
                .iter()
                .any(|p| p.starts_with("quarantine:frame-length-ambiguous:")),
            "{:?}",
            err.mark.provenance
        );
    }

    #[test]
    fn trailing_extra_byte_after_an_unmapped_frame_is_also_ambiguous() {
        // 同じ ambiguity 検査は unmapped msgid 経路（contract）でも同様に効く。
        let mut frame = GOLDEN_FRAME;
        frame[7] = 0x00; // 構造妥当・未マップ msgid
        let mut payload = frame.to_vec();
        payload.push(0xAA); // 余剰 1 バイト
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload,
            received_at: 0,
        };
        let err = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect_err("trailing extra byte on unmapped path must also be rejected");
        assert_eq!(err.mark.status, MarkStatus::Invalid);
        assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    }

    #[test]
    fn missing_crc_byte_is_ambiguous_not_silently_accepted() {
        // GOLDEN_FRAME から末尾 1 バイト（CRC の後半）を落とすと、宣言 len 分のペイロードは
        // 完備している（`payload_slice`/decode は通る）が、フレーム全体としては 1 バイト
        // 足りない＝境界が曖昧。旧実装は CRC を一切数えないため黙って通していた。
        let mut frame = GOLDEN_FRAME.to_vec();
        frame.pop();
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: frame,
            received_at: 0,
        };
        let err = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect_err("a short-by-one-byte frame must be rejected, not silently accepted");
        assert_eq!(err.mark.status, MarkStatus::Invalid);
        assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
    }

    #[test]
    fn quarantine_rejection_does_not_leak_through_the_wider_outcome_api() {
        // normalize_frame（3-outcome API）でも quarantine の拒否は Err（malformed とは別語彙）で
        // 一貫する。
        let mut frame = GOLDEN_FRAME.to_vec();
        frame.push(0xAA);
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: frame,
            received_at: 0,
        };
        let err = MavlinkNormalizer::new()
            .normalize_frame(&raw)
            .expect_err("ambiguous trailing bytes must not become MavlinkOutcome::Unmapped");
        assert_eq!(err.mark.reason_code, QUARANTINE_REASON_CODE);
        // sanity: 型として `MavlinkOutcome` を経由しないことを確認（unused-import 回避も兼ねる）。
        let _: fn(MavlinkOutcome) = |_| {};
    }
}

#[cfg(test)]
mod ec002_end_to_end {
    //! initial walking skeleton（end-to-end 1 本）:
    //! MAVLink2 GLOBAL_POSITION_INT バイト列 → COM PlatformState（normalize）→ CoT event XML。
    //!
    //! 南向き(adapter)→core→北向き(northbound) を 1 本貫通させ、CoT XML に期待 uid/lat/lon/hae
    //! が現れることを確認する（contract の e2e 面・X-NOWRITE: 経路に write/command を出さない）。

    use super::MavlinkNormalizer;
    use super::ec002::GOLDEN_FRAME;
    use musubi_core::{Normalizer, RawObservation};
    use musubi_northbound::{CotPublisher, NorthboundPublisher};
    use musubi_types::ComObject;

    #[test]
    fn mavlink_bytes_to_cot_xml() {
        let raw = RawObservation {
            source_id: "uav-01".to_string(),
            payload: GOLDEN_FRAME.to_vec(),
            received_at: 0,
        };

        // 南向き: raw → COM を内包した EvidenceEnvelope。
        let envelope = MavlinkNormalizer::new()
            .normalize(&raw)
            .expect("golden frame normalizes");

        // 北向き: EvidenceEnvelope → CoT event XML bytes。
        let publisher = CotPublisher::new();
        assert_eq!(publisher.dialect(), "cot");
        let xml = String::from_utf8(publisher.publish(&envelope).expect("publishes CoT"))
            .expect("utf8 xml");

        // 期待 uid（platform_id 由来・contract: source_id と sysid の合成で demux 済み）。
        assert!(xml.contains("uid=\"uav-01/sys1\""), "{xml}");
        assert!(xml.contains("<event "), "{xml}");
        assert!(xml.contains("<point "), "{xml}");

        // contract round-trip（source→COM→northbound→decode・fail-on-loss）:
        // CoT 属性を f64 に戻し、デコード済み COM の lat/lon/alt と許容誤差内で一致させる。
        let ComObject::PlatformState(ps) = &envelope.observation else {
            panic!("expected PlatformState")
        };
        let pos = ps.position.as_ref().expect("position present");
        assert!((attr_f64(&xml, "lat") - pos.lat_deg).abs() <= 1e-6, "{xml}");
        assert!((attr_f64(&xml, "lon") - pos.lon_deg).abs() <= 1e-6, "{xml}");
        assert!(
            (attr_f64(&xml, "hae") - pos.alt_m.expect("alt present")).abs() <= 1e-6,
            "{xml}"
        );
        // 既知 golden 値（十進固定小数・指数表記でない）。
        assert!(xml.contains("lat=\"47.397742\""), "{xml}");
        assert!(xml.contains("lon=\"8.545594\""), "{xml}");
        assert!(xml.contains("hae=\"488.0\""), "{xml}");
        // 注: X-NOWRITE の実強制は scripts/fitness/no_write_surface.sh（型に command/dispatch
        //     経路が無いことを CI で機械検証）。e2e の !contains("command") は tautological
        //     なので oracle として置かない（verify 指摘）。
    }

    /// テスト用: CoT XML から数値属性を抜く（round-trip 検証用・最小）。
    fn attr_f64(xml: &str, key: &str) -> f64 {
        let pat = format!("{key}=\"");
        let start = xml.find(&pat).expect("attr present") + pat.len();
        let rest = &xml[start..];
        let end = rest.find('"').expect("attr close");
        rest[..end].parse::<f64>().expect("attr parses as f64")
    }
}

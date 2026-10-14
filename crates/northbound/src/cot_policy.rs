//! `CoT` egress policy — 「product が出すと宣言した `CoT` だけを wire へ通す」fail-closed 検査層。
//!
//! serializer policy: 送出前に type／detail の意味を検査する層が product 側に存在せず、
//! [`CotPublisher::publish`](crate::CotPublisher) は組み立てた XML をそのまま返し、
//! `CotTcpSink::emit` は渡された bytes をそのまま送出していた。本モジュールが policy の
//! **唯一の正本**で、(1) publish の出口（construction gate）と (2) 各 emit 点（egress gate＝
//! file-output consumers）が **同じ関数**を呼ぶ
//! （policy を 2 箇所へ写経しない＝drift させない）。
//!
//! # allow-list の定義（the bounded output policy）
//!
//! allow-list は「product が出すと宣言した型集合」＝[`cot_type`](crate::cot_type) が返しうる
//! `a-u` / `a-u-A` / `a-u-S` / `a-u-G` の 4 つ。**lab（学習器具）の 4 型は product output の
//! 適合基準ではない**ので持ち込まない。結果として allow-list は全て affiliation `u` であり、
//! **contract(b)（affiliation 非断定）はここでも二重に固定される**——`a-f-*` / `a-h-*` は
//! 「宣言外」として送出前に拒否される（allow-list は断定の入口を塞ぐ側にしか働かない）。
//!
//! # remarks の wire 文法と escape 由来の境界破壊（serializer policy で閉じる穴）
//!
//! `<remarks>` は `MARK:…;CONF:…;PROV:…;BASIS:…` の **`;` 区切り 4 フィールド**（contract）。
//! ところが [`xml_escape_attr`](crate) は `&` → `&amp;` のように **`;` で終わる実体参照**を作るため、
//! `reason_code` / `provenance` に `& < > "` が入ると **wire 上のフィールド境界が壊れる**。
//! 既存の conformance `C-MARK-REASON` / `C-PROVENANCE` は `;` の直接混入しか見ておらず、この
//! escape 由来の `;` は素通りする。よって remarks 側は escape に頼らず **実体参照の存在自体を拒否**
//! し、トップレベル `;` がちょうど 3 個であることを強制する。
//!
//! # honest scope（over-claim 禁止）
//!
//! これは hand-rolled な **wire 検査**（zero-dep・AGENTS.md §3）であって XML parser でも XSD
//! validator でもない。「宣言した形以外を通さない」ための fail-closed な必要条件であり、
//! 「これを通れば TAK が受理する」ことは意味しない（実受理は external validation・contract）。

use core::fmt;

use crate::wire_policy::{
    self, FieldFault, GrammarFault, attr, is_two_decimal, open_tag, sanitize_reason,
};

/// product が出すと宣言した `CoT` event type の全集合（serializer policy 裁定 A）。
pub const DECLARED_COT_TYPES: [&str; 4] = ["a-u", "a-u-A", "a-u-S", "a-u-G"];
/// `<detail>` 直下に出すと宣言した子要素名（現行は `<remarks>` のみ）。
pub const DECLARED_DETAIL_TAGS: [&str; 1] = ["remarks"];
/// `<remarks>` の 4 フィールド固定 prefix（順序も固定・contract のワイヤ文法契約）。
pub const DECLARED_REMARKS_PREFIXES: [&str; 4] = ["MARK:", "CONF:", "PROV:", "BASIS:"];
/// `BASIS:` の閉集合（`fmt_basis` が返す 3 値・[`wire_policy`] と共有）。
pub const DECLARED_BASIS_TOKENS: [&str; 3] = wire_policy::DECLARED_BASIS_TOKENS;
/// wire に出してよい実体参照（`xml_escape_attr` が生成する 4 つだけ・[`wire_policy`] と共有）。
pub const DECLARED_ENTITIES: [&str; 4] = wire_policy::DECLARED_ENTITIES;

/// 1 event の最大バイト数（DoS 側の広い網・[`wire_policy`] と共有）。
///
/// 実測（seed 42 reference example・40 event）の最大は 790 バイト＝約 10 倍の余裕。上限は**意味検査では
/// なく**、超えたら黙って切り詰めず拒否する（no-silent-truncation）。
pub const MAX_EVENT_BYTES: usize = wire_policy::MAX_DOCUMENT_BYTES;
/// `uid` 属性値（escape 後）の最大文字数。実測最大 47 文字（`fpv-01#absence:…@…`）。
pub const MAX_UID_WIRE_CHARS: usize = 128;
/// `<remarks>` テキスト（escape 後）の最大文字数。実測最大 425 文字（absence + cause token 連結）。
pub const MAX_REMARKS_WIRE_CHARS: usize = 1_024;

/// policy 違反（拒否理由・no-silent-drop のため必ず理由を出す）。
///
/// `Display` は `;` を **含まない**文字列を返す（拒否理由自体を MARK／ログへ載せても contract の
/// トップレベル区切りと衝突しない）。違反 payload は **外部由来文字列そのもの**（拒否した type や
/// entity）なので、`;` と危険符号点を [`sanitize_reason`] で潰してから出す——さもないと
/// 「境界を壊す文字を拒否した理由」が同じ境界を壊すという同型の穴が開く。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CotPolicyViolation {
    /// wire bytes が UTF-8 でない（宣言 encoding と不一致）。
    NotUtf8,
    /// event 全体が [`MAX_EVENT_BYTES`] を超える。
    EventTooLarge { bytes: usize },
    /// `<event>` 要素が無い。
    NoEventElement,
    /// `<event>` の必須属性が無い。
    MissingAttr(&'static str),
    /// [`DECLARED_COT_TYPES`] に無い type（command-shaped `t-*`・非 atom・affiliation 断定を含む）。
    UndeclaredType { found: String },
    /// comment / DOCTYPE / CDATA / 2 つ目以降の processing instruction。
    ProhibitedMarkup { found: &'static str },
    /// [`DECLARED_ENTITIES`] 以外の実体参照（数値参照・任意 entity・remarks 内の実体参照）。
    UndeclaredEntity { found: String },
    /// `<detail>` 要素が無い。
    NoDetailElement,
    /// `<detail>` 直下に `<remarks>` が無い。
    NoRemarksElement,
    /// [`DECLARED_DETAIL_TAGS`] に無い detail 子要素。
    UndeclaredDetailTag { found: String },
    /// `<detail>` 直下の子要素数がちょうど 1 でない。
    DetailChildCount { found: usize },
    /// remarks のトップレベル `;` 区切りフィールド数が 4 でない。
    RemarksFieldCount { found: usize },
    /// remarks のフィールド prefix／順序が [`DECLARED_REMARKS_PREFIXES`] と違う。
    RemarksPrefix { index: usize, found: String },
    /// [`DECLARED_BASIS_TOKENS`] に無い BASIS token。
    UndeclaredBasis { found: String },
    /// `CONF:` が固定 2 桁小数でない（科学表記・NaN・負値）。
    MalformedConfidence { found: String },
    /// 外部由来 field に危険符号点（C0/C1・DEL・bidi・zero-width・BOM）。
    DisallowedCodepoint { field: &'static str, codepoint: u32 },
    /// 外部由来 field が上限文字数を超える（黙って切り詰めない）。
    FieldTooLong {
        field: &'static str,
        chars: usize,
        max: usize,
    },
    /// 外部由来 field が空／空白のみ。
    EmptyField(&'static str),
}

impl fmt::Display for CotPolicyViolation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotUtf8 => write!(f, "not-utf8"),
            Self::EventTooLarge { bytes } => {
                write!(f, "event-too-large:{bytes}:max={MAX_EVENT_BYTES}")
            }
            Self::NoEventElement => write!(f, "no-event-element"),
            Self::MissingAttr(attr) => write!(f, "missing-attr:{attr}"),
            Self::UndeclaredType { found } => {
                write!(f, "undeclared-type:{}", sanitize_reason(found))
            }
            Self::ProhibitedMarkup { found } => write!(f, "prohibited-markup:{found}"),
            Self::UndeclaredEntity { found } => {
                write!(f, "undeclared-entity:{}", sanitize_reason(found))
            }
            Self::NoDetailElement => write!(f, "no-detail-element"),
            Self::NoRemarksElement => write!(f, "no-remarks-element"),
            Self::UndeclaredDetailTag { found } => {
                write!(f, "undeclared-detail-tag:{}", sanitize_reason(found))
            }
            Self::DetailChildCount { found } => write!(f, "detail-child-count:{found}:expected=1"),
            Self::RemarksFieldCount { found } => {
                write!(f, "remarks-field-count:{found}:expected=4")
            }
            Self::RemarksPrefix { index, found } => {
                write!(f, "remarks-prefix:{index}:{}", sanitize_reason(found))
            }
            Self::UndeclaredBasis { found } => {
                write!(f, "undeclared-basis:{}", sanitize_reason(found))
            }
            Self::MalformedConfidence { found } => {
                write!(f, "malformed-confidence:{}", sanitize_reason(found))
            }
            Self::DisallowedCodepoint { field, codepoint } => {
                write!(f, "disallowed-codepoint:{field}:U+{codepoint:04X}")
            }
            Self::FieldTooLong { field, chars, max } => {
                write!(f, "field-too-long:{field}:{chars}:max={max}")
            }
            Self::EmptyField(field) => write!(f, "empty-field:{field}"),
        }
    }
}

/// wire へ出す直前の `CoT` event bytes を、**宣言した形だけ**に絞り込む（fail-closed）。
///
/// publish の出口と全 emit 点がこの 1 関数を呼ぶ。純関数（I/O なし・状態なし・決定論）。
///
/// # Errors
/// 宣言外 type・宣言外 detail tag・禁止 markup／実体参照・remarks 文法違反・危険符号点・
/// 長さ超過のいずれかで [`CotPolicyViolation`]。
pub fn check_cot_event_bytes(bytes: &[u8]) -> Result<(), CotPolicyViolation> {
    if bytes.len() > MAX_EVENT_BYTES {
        return Err(CotPolicyViolation::EventTooLarge { bytes: bytes.len() });
    }
    let xml = core::str::from_utf8(bytes).map_err(|_| CotPolicyViolation::NotUtf8)?;
    check_markup_floor(xml)?;

    let event = open_tag(xml, "event").ok_or(CotPolicyViolation::NoEventElement)?;

    // (1) type allow-list（本 issue の核・serializer policy 裁定 A）。
    let cot_type = attr(event, "type").ok_or(CotPolicyViolation::MissingAttr("type"))?;
    if !DECLARED_COT_TYPES.contains(&cot_type) {
        return Err(CotPolicyViolation::UndeclaredType {
            found: cot_type.to_string(),
        });
    }

    // (2) 外部由来文字列（uid ← platform_id）の charset／長さ。
    let uid = attr(event, "uid").ok_or(CotPolicyViolation::MissingAttr("uid"))?;
    check_wire_field("uid", uid, MAX_UID_WIRE_CHARS)?;

    // (3) detail 直下は宣言した tag（remarks）ちょうど 1 つだけ。
    let detail = detail_inner(xml).ok_or(CotPolicyViolation::NoDetailElement)?;
    check_detail_children(detail)?;

    // (4) remarks の 4 フィールド文法と、そこへ入る外部由来文字列。
    let remarks = element_text(detail, "remarks").ok_or(CotPolicyViolation::NoRemarksElement)?;
    check_wire_field("remarks", remarks, MAX_REMARKS_WIRE_CHARS)?;
    check_remarks_grammar(remarks)
}

/// 宣言していない markup（comment／DOCTYPE／CDATA／2 つ目以降の PI／未宣言実体参照）を弾く。
///
/// 判定は [`wire_policy`] 側の共有実体で、ここは `CoT` の wire 語彙への写像だけを持つ。
fn check_markup_floor(xml: &str) -> Result<(), CotPolicyViolation> {
    if let Some(found) = wire_policy::prohibited_markup(xml) {
        return Err(CotPolicyViolation::ProhibitedMarkup { found });
    }
    if let Some(found) = wire_policy::first_undeclared_entity(xml) {
        return Err(CotPolicyViolation::UndeclaredEntity { found });
    }
    Ok(())
}

/// `<detail>` の内側（開始タグ直後〜`</detail>` 直前）。
fn detail_inner(xml: &str) -> Option<&str> {
    let start = xml.find("<detail")?;
    let head_end = xml[start..].find('>')? + start + 1;
    let end = xml[head_end..].find("</detail>")? + head_end;
    Some(&xml[head_end..end])
}

/// `<detail>` 直下に現れてよい tag が [`DECLARED_DETAIL_TAGS`] だけで、かつ 1 個であること。
fn check_detail_children(inner: &str) -> Result<(), CotPolicyViolation> {
    let mut opened = 0usize;
    for (i, _) in inner.match_indices('<') {
        let name: String = inner[i + 1..]
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '/'))
            .collect();
        let bare = name.trim_start_matches('/');
        if !DECLARED_DETAIL_TAGS.contains(&bare) {
            return Err(CotPolicyViolation::UndeclaredDetailTag {
                found: bare.to_string(),
            });
        }
        if !name.starts_with('/') {
            opened += 1;
        }
    }
    if opened != 1 {
        return Err(CotPolicyViolation::DetailChildCount { found: opened });
    }
    Ok(())
}

/// `<remarks>` の 4 フィールド固定文法（serializer policy の参照実装＝現行 wire 契約）。
///
/// 分割・実体参照拒否・prefix 照合は [`wire_policy::inspect_semicolon_fields`] が持つ共有実体
/// （NVG `point@modifiers` と**同じ関数**）。ここは `CoT` の wire 語彙への写像と、`CoT` 固有の
/// フィールド位置（`CONF:` は index 1・`BASIS:` は index 3）の値検査だけを持つ。
fn check_remarks_grammar(remarks: &str) -> Result<(), CotPolicyViolation> {
    let fields = wire_policy::inspect_semicolon_fields(remarks, &DECLARED_REMARKS_PREFIXES)
        .map_err(|fault| match fault {
            GrammarFault::Entity { found } => CotPolicyViolation::UndeclaredEntity { found },
            GrammarFault::FieldCount { found } => CotPolicyViolation::RemarksFieldCount { found },
            GrammarFault::Prefix { index, found } => {
                CotPolicyViolation::RemarksPrefix { index, found }
            }
        })?;
    let conf = &fields[1][DECLARED_REMARKS_PREFIXES[1].len()..];
    if !is_two_decimal(conf) {
        return Err(CotPolicyViolation::MalformedConfidence {
            found: conf.to_string(),
        });
    }
    let basis = &fields[3][DECLARED_REMARKS_PREFIXES[3].len()..];
    if !DECLARED_BASIS_TOKENS.contains(&basis) {
        return Err(CotPolicyViolation::UndeclaredBasis {
            found: basis.to_string(),
        });
    }
    Ok(())
}

/// 外部由来文字列が入る wire field の共通検査（空・長さ・危険符号点）。
///
/// 判定は [`wire_policy::inspect_wire_field`]（NVG `uri`／`label`／`modifiers` と共有）。
fn check_wire_field(
    field: &'static str,
    value: &str,
    max_chars: usize,
) -> Result<(), CotPolicyViolation> {
    wire_policy::inspect_wire_field(value, max_chars).map_err(|fault| match fault {
        FieldFault::Empty => CotPolicyViolation::EmptyField(field),
        FieldFault::TooLong { chars, max } => {
            CotPolicyViolation::FieldTooLong { field, chars, max }
        }
        FieldFault::DisallowedCodepoint { codepoint } => {
            CotPolicyViolation::DisallowedCodepoint { field, codepoint }
        }
    })
}

/// `<name>…</name>` のテキスト。
fn element_text<'a>(scope: &'a str, name: &str) -> Option<&'a str> {
    let open = format!("<{name}>");
    let close = format!("</{name}>");
    let start = scope.find(&open)? + open.len();
    let end = scope[start..].find(&close)? + start;
    Some(&scope[start..end])
}

#[cfg(test)]
mod tests {
    //! serializer policy の oracle: 「宣言した形だけを通す」境界を決定論で固定する。
    //!
    //! baseline（`declared_baseline`）が **通る**ことを毎回確かめてから変異を当てる＝負例が
    //! 「常に fail」でないことを担保する（`cot_tak_validity.rs::validity_predicate_rejects_broken_cot`
    //! と同じ teeth の付け方）。

    use super::{
        CotPolicyViolation, DECLARED_BASIS_TOKENS, DECLARED_COT_TYPES, DECLARED_ENTITIES,
        DECLARED_REMARKS_PREFIXES, MAX_EVENT_BYTES, MAX_REMARKS_WIRE_CHARS, MAX_UID_WIRE_CHARS,
        check_cot_event_bytes,
    };
    // serializer policy で共有プリミティブへ移した符号点表。drift-guard test（`musubi_core` との一致検査）は
    // serializer policy で書かれた本 module に置いたまま、実体だけ `wire_policy` を指す。
    use crate::wire_policy::is_disallowed_codepoint;

    /// publisher の出力と同じ骨格を持つ、policy を通る baseline。
    fn declared_baseline() -> String {
        [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
            "<event version=\"2.0\" uid=\"my-device-01\" type=\"a-u\" how=\"m-g\" ",
            "time=\"1970-01-01T00:00:00.000Z\" start=\"1970-01-01T00:00:00.000Z\" ",
            "stale=\"1970-01-01T00:00:15.000Z\">\n  ",
            "<point lat=\"35.6586\" lon=\"139.7454\" hae=\"12.0\" ce=\"9999999.0\" le=\"9999999.0\"/>\n  ",
            "<detail>\n    ",
            "<remarks>MARK:OK:normalized;CONF:0.60;PROV:source:my-device-01;BASIS:adapter-assigned</remarks>\n  ",
            "</detail>\n</event>\n",
        ]
        .concat()
    }

    fn check(xml: &str) -> Result<(), CotPolicyViolation> {
        check_cot_event_bytes(xml.as_bytes())
    }

    #[test]
    fn declared_baseline_passes() {
        assert_eq!(check(&declared_baseline()), Ok(()));
    }

    #[test]
    fn command_shaped_type_is_refused() {
        // §5 原則 1 の接地点: tasking/command 形の type は product が宣言していない。
        let xml = declared_baseline().replace("type=\"a-u\"", "type=\"t-x-c-c\"");
        assert_eq!(
            check(&xml),
            Err(CotPolicyViolation::UndeclaredType {
                found: "t-x-c-c".to_string()
            })
        );
    }

    #[test]
    fn non_atom_type_is_refused() {
        let xml = declared_baseline().replace("type=\"a-u\"", "type=\"b-m-p-s-p-loc\"");
        assert!(matches!(
            check(&xml),
            Err(CotPolicyViolation::UndeclaredType { .. })
        ));
    }

    #[test]
    fn lab_only_types_are_not_product_types() {
        // serializer policy 裁定 A の pin: lab の 4 型は **学習器具専用の安全制御**であって product output の
        // 適合基準ではない。product 側 allow-list に入っていないことを固定する。
        for lab in ["a-f-A-M-F", "a-u-A-M-F", "a-h-G-U-C", "a-f-G-U-C"] {
            let xml = declared_baseline().replace("type=\"a-u\"", &format!("type=\"{lab}\""));
            assert!(
                matches!(check(&xml), Err(CotPolicyViolation::UndeclaredType { .. })),
                "lab type {lab} must not be a product type"
            );
        }
    }

    #[test]
    fn affiliation_asserting_types_are_refused() {
        // contract(b): allow-list は affiliation 断定の入口を塞ぐ側にしか働かない。
        for asserted in ["a-f-G", "a-h-A", "a-n-S", "a-f", "a-h"] {
            let xml = declared_baseline().replace("type=\"a-u\"", &format!("type=\"{asserted}\""));
            assert!(
                matches!(check(&xml), Err(CotPolicyViolation::UndeclaredType { .. })),
                "affiliation-asserting type {asserted} must be refused"
            );
        }
        assert!(
            DECLARED_COT_TYPES.iter().all(|t| t.starts_with("a-u")),
            "every declared type must keep affiliation unknown: {DECLARED_COT_TYPES:?}"
        );
    }

    #[test]
    fn undeclared_detail_tag_is_refused() {
        let xml = declared_baseline().replace(
            "</remarks>\n  ",
            "</remarks>\n    <link uid=\"other\"/>\n  ",
        );
        assert_eq!(
            check(&xml),
            Err(CotPolicyViolation::UndeclaredDetailTag {
                found: "link".to_string()
            })
        );
    }

    #[test]
    fn duplicated_or_missing_remarks_is_refused() {
        let dup = declared_baseline().replace(
            "</remarks>\n  ",
            "</remarks>\n    <remarks>MARK:OK:x;CONF:0.10;PROV:y;BASIS:measured</remarks>\n  ",
        );
        assert_eq!(
            check(&dup),
            Err(CotPolicyViolation::DetailChildCount { found: 2 })
        );

        let empty = declared_baseline().replace(
            "<remarks>MARK:OK:normalized;CONF:0.60;PROV:source:my-device-01;BASIS:adapter-assigned</remarks>\n  ",
            "",
        );
        assert_eq!(
            check(&empty),
            Err(CotPolicyViolation::DetailChildCount { found: 0 })
        );
    }

    #[test]
    fn pi_comment_doctype_and_cdata_are_refused() {
        let comment = declared_baseline().replace("<detail>", "<!-- injected --><detail>");
        assert_eq!(
            check(&comment),
            Err(CotPolicyViolation::ProhibitedMarkup { found: "<!" })
        );

        let doctype =
            declared_baseline().replace("<event ", "<!DOCTYPE event [<!ENTITY x \"y\">]>\n<event ");
        assert_eq!(
            check(&doctype),
            Err(CotPolicyViolation::ProhibitedMarkup { found: "<!" })
        );

        let cdata = declared_baseline().replace("MARK:OK", "<![CDATA[x]]>MARK:OK");
        assert_eq!(
            check(&cdata),
            Err(CotPolicyViolation::ProhibitedMarkup { found: "<!" })
        );

        let pi = declared_baseline().replace("<detail>", "<?php echo 1;?><detail>");
        assert_eq!(
            check(&pi),
            Err(CotPolicyViolation::ProhibitedMarkup { found: "<?" })
        );
    }

    #[test]
    fn numeric_and_custom_entities_are_refused() {
        let numeric = declared_baseline().replace("my-device-01\" type", "my-device&#x41;\" type");
        assert!(matches!(
            check(&numeric),
            Err(CotPolicyViolation::UndeclaredEntity { .. })
        ));

        let custom = declared_baseline().replace("my-device-01\" type", "&xxe;\" type");
        assert!(matches!(
            check(&custom),
            Err(CotPolicyViolation::UndeclaredEntity { .. })
        ));
    }

    #[test]
    fn declared_entities_in_uid_still_pass() {
        // escape で安全にできるものは拒否せず escape する（E-1・既存 `uid_is_xml_escaped` と整合）。
        let xml =
            declared_baseline().replace("uid=\"my-device-01\"", "uid=\"a&amp;b&lt;c&gt;&quot;d\"");
        assert_eq!(check(&xml), Ok(()));
        assert_eq!(DECLARED_ENTITIES.len(), 4);
    }

    #[test]
    fn entity_in_remarks_breaks_field_boundary_and_is_refused() {
        // serializer policy で閉じる穴: `&` を escape すると `&amp;` の `;` が 4 フィールド境界を壊す。
        // uid と違い remarks 側は escape に頼らず **拒否**する（E-2）。
        let xml = declared_baseline().replace("PROV:source:my-device-01", "PROV:source:a&amp;b");
        assert!(matches!(
            check(&xml),
            Err(CotPolicyViolation::UndeclaredEntity { .. })
        ));
    }

    #[test]
    fn remarks_with_extra_top_level_separator_is_refused() {
        let xml = declared_baseline().replace("MARK:OK:normalized", "MARK:OK:a;b");
        assert_eq!(
            check(&xml),
            Err(CotPolicyViolation::RemarksFieldCount { found: 5 })
        );
    }

    #[test]
    fn remarks_field_order_is_fixed() {
        let xml = declared_baseline().replace(
            "MARK:OK:normalized;CONF:0.60",
            "CONF:0.60;MARK:OK:normalized",
        );
        assert!(matches!(
            check(&xml),
            Err(CotPolicyViolation::RemarksPrefix { index: 0, .. })
        ));
        assert_eq!(DECLARED_REMARKS_PREFIXES.len(), 4);
    }

    #[test]
    fn undeclared_basis_token_is_refused() {
        let xml = declared_baseline().replace("BASIS:adapter-assigned", "BASIS:trust-me");
        assert_eq!(
            check(&xml),
            Err(CotPolicyViolation::UndeclaredBasis {
                found: "trust-me".to_string()
            })
        );
        for token in DECLARED_BASIS_TOKENS {
            let ok =
                declared_baseline().replace("BASIS:adapter-assigned", &format!("BASIS:{token}"));
            assert_eq!(check(&ok), Ok(()), "declared basis {token} must pass");
        }
    }

    #[test]
    fn malformed_confidence_is_refused() {
        for bad in ["6e-1", "0.6", "NaN", "-0.50", ".60"] {
            let xml = declared_baseline().replace("CONF:0.60", &format!("CONF:{bad}"));
            assert!(
                matches!(
                    check(&xml),
                    Err(CotPolicyViolation::MalformedConfidence { .. })
                ),
                "CONF:{bad} must be refused"
            );
        }
    }

    #[test]
    fn control_and_bidi_codepoints_in_uid_are_refused() {
        for (cp, c) in [
            (0x0007u32, '\u{0007}'),
            (0x0009, '\u{0009}'),
            (0x202E, '\u{202E}'),
            (0x200B, '\u{200B}'),
            (0xFEFF, '\u{FEFF}'),
        ] {
            let xml =
                declared_baseline().replace("my-device-01\" type", &format!("dev{c}01\" type"));
            assert_eq!(
                check(&xml),
                Err(CotPolicyViolation::DisallowedCodepoint {
                    field: "uid",
                    codepoint: cp
                }),
                "U+{cp:04X} must be refused in uid"
            );
        }
    }

    #[test]
    fn non_ascii_uid_is_allowed() {
        // 意図的判断: ASCII 限定にはしない（民生現場の日本語資産名を殺さない）。危険クラスだけ落とす。
        let xml = declared_baseline().replace("my-device-01", "農場-01");
        assert_eq!(check(&xml), Ok(()));
    }

    #[test]
    fn oversize_event_and_fields_are_refused() {
        let big = "x".repeat(MAX_EVENT_BYTES + 1);
        assert_eq!(
            check_cot_event_bytes(big.as_bytes()),
            Err(CotPolicyViolation::EventTooLarge {
                bytes: MAX_EVENT_BYTES + 1
            })
        );

        let long_uid = "u".repeat(MAX_UID_WIRE_CHARS + 1);
        let xml = declared_baseline().replace("my-device-01\" type", &format!("{long_uid}\" type"));
        assert_eq!(
            check(&xml),
            Err(CotPolicyViolation::FieldTooLong {
                field: "uid",
                chars: MAX_UID_WIRE_CHARS + 1,
                max: MAX_UID_WIRE_CHARS
            })
        );

        let filler = "z".repeat(MAX_REMARKS_WIRE_CHARS);
        let xml = declared_baseline().replace("MARK:OK:normalized", &format!("MARK:OK:{filler}"));
        assert!(matches!(
            check(&xml),
            Err(CotPolicyViolation::FieldTooLong {
                field: "remarks",
                ..
            })
        ));
    }

    #[test]
    fn empty_uid_is_refused() {
        let xml = declared_baseline().replace("uid=\"my-device-01\"", "uid=\"\"");
        assert_eq!(check(&xml), Err(CotPolicyViolation::EmptyField("uid")));
    }

    #[test]
    fn not_utf8_is_refused() {
        assert_eq!(
            check_cot_event_bytes(&[0xff, 0xfe, 0x00]),
            Err(CotPolicyViolation::NotUtf8)
        );
    }

    #[test]
    fn missing_event_or_attrs_is_refused() {
        assert_eq!(
            check_cot_event_bytes(b"<nothing/>"),
            Err(CotPolicyViolation::NoEventElement)
        );
        assert_eq!(
            check_cot_event_bytes(b"<event uid=\"a\"/>"),
            Err(CotPolicyViolation::MissingAttr("type"))
        );
        assert_eq!(
            check_cot_event_bytes(b"<event type=\"a-u\"/>"),
            Err(CotPolicyViolation::MissingAttr("uid"))
        );
        assert_eq!(
            check_cot_event_bytes(b"<event type=\"a-u\" uid=\"a\"/>"),
            Err(CotPolicyViolation::NoDetailElement)
        );
    }

    #[test]
    fn disallowed_codepoints_match_core_quarantine() {
        // drift-guard: northbound の runtime dep を types のみに保つため写した述語が、ingress 側
        // （`musubi_core::quarantine`・dev-dep）と **完全一致**することを機械確認する。
        let probes: Vec<u32> = (0x00..=0xA5u32)
            .chain([
                0x061B, 0x061C, 0x061D, 0x200A, 0x200B, 0x200F, 0x2010, 0x2029, 0x202A, 0x202E,
                0x202F, 0x205F, 0x2060, 0x2061, 0x2065, 0x2066, 0x2069, 0x206A, 0xFEFE, 0xFEFF,
                0xFF00, 0x1F600,
            ])
            .collect();
        for cp in probes {
            let Some(c) = char::from_u32(cp) else {
                continue;
            };
            assert_eq!(
                is_disallowed_codepoint(c),
                musubi_core::quarantine::is_disallowed_codepoint(c),
                "egress mirror drifted from ingress quarantine at U+{cp:04X}"
            );
        }
    }

    #[test]
    fn violation_display_never_contains_top_level_separator() {
        // 拒否理由を MARK／ログへ載せても contract の `;` 区切りを壊さない。
        let all = [
            CotPolicyViolation::NotUtf8,
            CotPolicyViolation::EventTooLarge { bytes: 1 },
            CotPolicyViolation::NoEventElement,
            CotPolicyViolation::MissingAttr("type"),
            CotPolicyViolation::UndeclaredType {
                // 外部由来 payload に `;` と制御文字を混ぜた最悪ケース（sanitize_reason の oracle）。
                found: "t-x-c-c;CONF:9.99\u{0007}".to_string(),
            },
            CotPolicyViolation::ProhibitedMarkup { found: "<!" },
            CotPolicyViolation::UndeclaredEntity {
                found: "&amp;b;CONF:".to_string(),
            },
            CotPolicyViolation::NoDetailElement,
            CotPolicyViolation::NoRemarksElement,
            CotPolicyViolation::UndeclaredDetailTag {
                found: "link".to_string(),
            },
            CotPolicyViolation::DetailChildCount { found: 2 },
            CotPolicyViolation::RemarksFieldCount { found: 5 },
            CotPolicyViolation::RemarksPrefix {
                index: 0,
                found: "CONF:0.60".to_string(),
            },
            CotPolicyViolation::UndeclaredBasis {
                found: "trust-me".to_string(),
            },
            CotPolicyViolation::MalformedConfidence {
                found: "6e-1".to_string(),
            },
            CotPolicyViolation::DisallowedCodepoint {
                field: "uid",
                codepoint: 0x202E,
            },
            CotPolicyViolation::FieldTooLong {
                field: "uid",
                chars: 129,
                max: 128,
            },
            CotPolicyViolation::EmptyField("uid"),
        ];
        for v in all {
            let text = v.to_string();
            assert!(!text.is_empty(), "violation must state a reason");
            assert!(
                !text.contains(';'),
                "reason must not carry the contract top-level separator: {text:?}"
            );
            assert!(
                !text.chars().any(is_disallowed_codepoint),
                "reason must not carry a disallowed codepoint: {text:?}"
            );
        }
    }
}

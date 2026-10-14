//! NVG egress policy — 「product が出すと宣言した NVG だけを wire へ通す」fail-closed 検査層。
//!
//! serializer policy: serializer policy（CoT・serializer policy）で塞いだ **escape 由来のフィールド境界破壊**が NVG 側に
//! そのまま残っていた。[`NvgPublisher::publish`](crate::NvgPublisher) は組み立てた XML を
//! そのまま返すだけで、`impl NorthboundPublisher for NvgPublisher` の中に policy 検査は **0 件**
//! だった。本モジュールが NVG policy の**唯一の正本**で、publish の出口（construction gate）が
//! これを呼ぶ。将来 NVG に固有の emit 点（sink／writer）が増えたら、**写経せず同じ関数**を呼ぶ
//! こと——それが serializer policy の採った「policy 実体は 1 つ、enforcement point は複数」という構造である。
//!
//! # `modifiers` の wire 文法と escape 由来の境界破壊（serializer policy で閉じる穴）
//!
//! `point@modifiers` は `MARK:…;REASON:…;CONF:…;BASIS:…` の **`;` 区切り 4 フィールド**（contract）。
//! ところが [`xml_escape_attr`](crate) は `&` → `&amp;` のように **`;` で終わる実体参照**を作るため、
//! `reason_code` に `& < > "` が入ると **wire 上のフィールド境界が壊れる**。serializer policy の実測:
//!
//! | `reason_code` | wire 上の `modifiers` | `;` 分割数 |
//! |---|---|---|
//! | `link-loss` | `MARK:DEGRADED;REASON:link-loss;CONF:0.60;BASIS:adapter-assigned` | 4（正常） |
//! | `parse&fail` | `MARK:DEGRADED;REASON:parse&amp;fail;CONF:0.60;BASIS:adapter-assigned` | **5** |
//!
//! **`&` か `<` が 1 文字入るだけで 4 フィールドが 5 に割れる。** 既存の conformance
//! `C-MARK-REASON` / `C-PROVENANCE` は `;` の直接混入しか見ておらず、この escape 由来の `;` は
//! 素通りする。よって escape に頼らず **実体参照の存在自体を拒否**する。
//!
//! # `label` に実体参照を許す理由（serializer policy の issue 記述からの意図的な逸脱）
//!
//! serializer policy は「`label` も同型」として実体参照拒否を `label` にも広げるよう書いているが、
//! **本実装は `label` では実体参照を拒否しない**（charset と長さだけを縛る）。理由は 3 つ:
//!
//! 1. **escape は `label` の境界を割らない。** `point@label` は `{platform_id} [{mark_summary}]`
//!    で、区切りは `;` ではなく `[ ]` である。`xml_escape_attr` が生むのは
//!    `&amp;` `&lt;` `&gt;` `&quot;` の 4 種だけで **`[` も `]` も含まない**ため、
//!    実体参照をいくら流し込んでも `label` の境界は割れない。XML の属性値として
//!    lossless に往復する。つまり issue が要求した実体参照拒否は、`label` については
//!    **守るべき境界に対して効かない**。
//! 2. **serializer policy の実際の設計がそうなっている。** `CoT` 側 policy が実体参照を拒否するのは
//!    `<remarks>`（`;` 区切り）だけで、**同じ `platform_id` を運ぶ `uid` 属性は escape を許す**
//!    （`cot::uid_is_xml_escaped` が serializer policy マージ後も通っている）。`uri`／`label` は `uid` と
//!    同じ役割なので、同じ扱いにするのが serializer policy の規則の忠実な適用である。
//! 3. **方言間で挙動を割らないため。** ここで NVG だけ厳しくすると、`&` を含む `platform_id` の
//!    envelope が **`CoT` へは publish できて NVG へはできない**という非対称が生まれ、`fan_out` が
//!    片方だけ `SinkOutcome::Failed` を返す。同じ観測が方言によって出たり出なかったりする方が
//!    運用上の危険が大きい。
//!
//! 結果として `platform_id` に `& < > "` が入った場合は **escape されて通る**（`CoT` `uid` と同じ）。
//! `reason_code` に入った場合は `modifiers` の実体参照拒否で **止まる**——そちらが実際に境界を
//! 割る側だからである。`label` にも `reason_code` は入るが、`modifiers` が先に fail-closed する。
//!
//! # ⚠️ `label` の `[ ]` 境界は**本 module では塞いでいない**（別 issue）
//!
//! 上の理由 1 は「実体参照は `label` の境界を割らない」までしか言っていない。
//! **`platform_id` が `[` `]` を直接含む場合は割れる**——escape を一切経由しない別経路である。
//!
//! ```text
//! platform_id = "UAV [FIRE:engage] X"
//!   -> label = "UAV [FIRE:engage] X [OK:normalized]"
//! ```
//!
//! 地図ラベルを読む人間には、`mark_summary` が入るはずの括弧位置に別の文字列が見える。
//! `platform_id` は adapter 由来の非信頼入力（`adapter-usv-json` の `vehicle_id` 等）なので
//! 机上の話ではない。
//!
//! 本 module が `label` に掛けているのは [`check_wire_field`] だけで、**空・長さ・危険符号点**
//! しか見ない。`modifiers` には [`check_modifiers_grammar`] があるのに `label` には対応する
//! 構造検査が無い——**この非対称が実際の欠陥**であり、serializer policy の `;` 境界とは別クラスである。
//!
//! `[` を `platform_id` で禁じるのか `label` 全体を文法検査するのかは設計判断を要するため、
//! serializer policy（`modifiers` の `;` 境界）とは分けて別 issue で扱う。
//!
//! # 判定の実体は共有されている（写経しない）
//!
//! 符号点表・`;` 分割・実体参照検出・固定 2 桁小数・markup floor は [`wire_policy`] に 1 つだけ
//! 置き、CoT policy と**同じ関数**を呼ぶ。本モジュールが持つのは NVG 固有の語彙（宣言 namespace／
//! version／symbol／フィールド prefix）と、共有の中間表現を NVG の wire 語彙へ写す薄い層だけ。
//! `sanitize_reason` を写経しないことが特に重要で、片方だけ `;` を落とせば「境界を壊す入力を
//! 拒否した理由」がまた境界を壊すという同型の穴が方言をまたいで再生産される。
//!
//! # honest scope（over-claim 禁止）
//!
//! これは hand-rolled な **wire 検査**（zero-dep・AGENTS.md §3）であって XML parser でも XSD
//! validator でもない。「宣言した形以外を通さない」ための fail-closed な必要条件であり、
//! 「これを通れば実 NVG 実装／Delta が受理する」ことは意味しない（実受理は external validation・contract）。

use core::fmt;

use crate::wire_policy::{
    self, FieldFault, GrammarFault, attr, is_two_decimal, open_tag, sanitize_reason,
};

/// NVG namespace URI（2.0.x 系共通・ADatcontract ベースライン）。
///
/// serializer（[`NvgPublisher`](crate::NvgPublisher)）と policy が**同じ定数**を使う
/// （2 箇所に literal を置いて drift させない）。
pub const NVG_NS: &str = "https://tide.act.nato.int/schemas/2012/10/nvg";

/// NVG version 属性文字列（2.0.2 はデータモデル改定だが version 文字列は 2.0.0 が実装標準）。
pub const NVG_VERSION: &str = "2.0.0";

/// product が出すと宣言した `point@symbol`（APcontractB SIDC）の全集合。
///
/// `app6b_symbol` が `PlatformDomain` 4 変種から生成しうる 4 つだけ。**全て affiliation `U`
/// （Unknown）**であり、contract(b)（Musubi は敵味方を判定しない）がここでも二重に固定される——
/// `SF*`（friend）／`SH*`（hostile）は「宣言外」として送出前に拒否される。
/// 過不足は `nvg_symbol_allow_list_matches_serializer` が機械確認する。
pub const DECLARED_NVG_SYMBOLS: [&str; 4] = [
    "app6b:SU-P------*****",
    "app6b:SUAP------*****",
    "app6b:SUSP------*****",
    "app6b:SUGP------*****",
];

/// `point@modifiers` の 4 フィールド固定 prefix（順序も固定・contract のワイヤ文法契約）。
///
/// `CoT` `<remarks>`（`MARK:／CONF:／PROV:／BASIS:`）とは **並びが違う**点に注意——NVG は
/// `REASON:` を独立フィールドとして 2 番目に持つ。だからこそ prefix 表は方言ごとに別で、
/// 分割・照合ロジックだけを共有する。
pub const DECLARED_MODIFIER_PREFIXES: [&str; 4] = ["MARK:", "REASON:", "CONF:", "BASIS:"];

/// `<point>` 直下に出すと宣言した子要素名（現行は `<TimeStamp>` のみ）。
/// `label` の `[…]` に出すと宣言した MARK status 語（`mark_status_label` の全変種）。
///
/// `label` は `{uri} [{status}]` または `{uri} [{status}:{reason_code}]` の合成である。
/// 過不足は `nvg_label_status_allow_list_matches_serializer` が機械確認する。
pub const DECLARED_MARK_STATUS: [&str; 8] = [
    "OK",
    "DEGRADED",
    "INVALID",
    "ORDER_UNKNOWN",
    "WITHHELD",
    "STALE_REVOCATION",
    "SELF_ASSERTED_TIME",
    "BUFFER_SATURATED",
];

/// `label` の `[…]` 境界に使う文字。`uri`（＝`platform_id`）がこれを含むと境界が曖昧になる。
pub const LABEL_BOUNDARY_CHARS: [char; 2] = ['[', ']'];

pub const DECLARED_POINT_TAGS: [&str; 1] = ["TimeStamp"];

/// `BASIS:` の閉集合（`fmt_basis` が返す 3 値・[`wire_policy`] と共有）。
pub const DECLARED_BASIS_TOKENS: [&str; 3] = wire_policy::DECLARED_BASIS_TOKENS;

/// wire に出してよい実体参照（`xml_escape_attr` が生成する 4 つだけ・[`wire_policy`] と共有）。
pub const DECLARED_ENTITIES: [&str; 4] = wire_policy::DECLARED_ENTITIES;

/// 1 document の最大バイト数（DoS 側の広い網・[`wire_policy`] と共有）。
pub const MAX_DOCUMENT_BYTES: usize = wire_policy::MAX_DOCUMENT_BYTES;

/// `uri` 属性値（escape 後）の最大文字数。CoT `uid` と**同じ外部由来文字列**（`platform_id`）
/// を運ぶので同じ上限にする。
pub const MAX_URI_WIRE_CHARS: usize = 128;

/// `label` 属性値（escape 後）の最大文字数。`platform_id` + MARK summary を運ぶ。
/// `CoT` `<remarks>` と同じ order の上限（超えたら黙って切り詰めず拒否＝no-silent-truncation）。
pub const MAX_LABEL_WIRE_CHARS: usize = 1_024;

/// `modifiers` 属性値（escape 後）の最大文字数。CoT `<remarks>` と同じ上限。
pub const MAX_MODIFIERS_WIRE_CHARS: usize = 1_024;

/// policy 違反（拒否理由・no-silent-drop のため必ず理由を出す）。
///
/// `Display` は `;` を **含まない**文字列を返す（拒否理由自体を MARK／ログへ載せても contract の
/// トップレベル区切りと衝突しない）。違反 payload は **外部由来文字列そのもの**（拒否した symbol や
/// entity）なので、`;` と危険符号点を [`sanitize_reason`] で潰してから出す——さもないと
/// 「境界を壊す文字を拒否した理由」が同じ境界を壊すという同型の穴が開く（serializer policy の教訓・完了条件）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NvgPolicyViolation {
    /// wire bytes が UTF-8 でない（宣言 encoding と不一致）。
    NotUtf8,
    /// document 全体が [`MAX_DOCUMENT_BYTES`] を超える。
    DocumentTooLarge { bytes: usize },
    /// `<nvg>` 要素が無い。
    NoNvgElement,
    /// `<point>` 要素が無い。
    NoPointElement,
    /// 必須属性が無い。
    MissingAttr(&'static str),
    /// 宣言外の namespace（[`NVG_NS`] 以外）。
    UndeclaredNamespace { found: String },
    /// 宣言外の version（[`NVG_VERSION`] 以外）。
    UndeclaredVersion { found: String },
    /// [`DECLARED_NVG_SYMBOLS`] に無い symbol（affiliation 断定を含む）。
    UndeclaredSymbol { found: String },
    /// comment / DOCTYPE / CDATA / 2 つ目以降の processing instruction。
    ProhibitedMarkup { found: &'static str },
    /// [`DECLARED_ENTITIES`] 以外の実体参照（数値参照・任意 entity）。
    UndeclaredEntity { found: String },
    /// 外部由来 field（`label` / `modifiers`）の中に実体参照が現れた＝境界が割れうる。
    EscapedFieldContent { field: &'static str, found: String },
    /// [`DECLARED_POINT_TAGS`] に無い `<point>` 子要素。
    UndeclaredPointTag { found: String },
    /// `<point>` 直下の子要素数がちょうど 1 でない。
    PointChildCount { found: usize },
    /// `label` が `{uri} [{mark_summary}]` の形をしていない（serializer policy）。
    LabelShape { found: String },
    /// `label` の `[…]` に宣言外の MARK status が入っている（serializer policy）。
    UndeclaredMarkStatus { found: String },
    /// `uri`（＝`platform_id`）が `[` / `]` を含み、`label` の境界が曖昧になる（serializer policy）。
    UriBreaksLabelBoundary { found: String },
    /// `modifiers` のトップレベル `;` 区切りフィールド数が 4 でない（serializer policy の核）。
    ModifiersFieldCount { found: usize },
    /// `modifiers` のフィールド prefix／順序が [`DECLARED_MODIFIER_PREFIXES`] と違う。
    ModifiersPrefix { index: usize, found: String },
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

impl fmt::Display for NvgPolicyViolation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotUtf8 => write!(f, "not-utf8"),
            Self::DocumentTooLarge { bytes } => {
                write!(f, "document-too-large:{bytes}:max={MAX_DOCUMENT_BYTES}")
            }
            Self::NoNvgElement => write!(f, "no-nvg-element"),
            Self::NoPointElement => write!(f, "no-point-element"),
            Self::MissingAttr(attr) => write!(f, "missing-attr:{attr}"),
            Self::UndeclaredNamespace { found } => {
                write!(f, "undeclared-namespace:{}", sanitize_reason(found))
            }
            Self::UndeclaredVersion { found } => {
                write!(f, "undeclared-version:{}", sanitize_reason(found))
            }
            Self::UndeclaredSymbol { found } => {
                write!(f, "undeclared-symbol:{}", sanitize_reason(found))
            }
            Self::ProhibitedMarkup { found } => write!(f, "prohibited-markup:{found}"),
            Self::UndeclaredEntity { found } => {
                write!(f, "undeclared-entity:{}", sanitize_reason(found))
            }
            Self::EscapedFieldContent { field, found } => {
                write!(
                    f,
                    "escaped-field-content:{field}:{}",
                    sanitize_reason(found)
                )
            }
            Self::LabelShape { found } => {
                write!(f, "label-shape:{}", sanitize_reason(found))
            }
            Self::UndeclaredMarkStatus { found } => {
                write!(f, "undeclared-mark-status:{}", sanitize_reason(found))
            }
            Self::UriBreaksLabelBoundary { found } => {
                write!(f, "uri-breaks-label-boundary:{}", sanitize_reason(found))
            }
            Self::UndeclaredPointTag { found } => {
                write!(f, "undeclared-point-tag:{}", sanitize_reason(found))
            }
            Self::PointChildCount { found } => write!(f, "point-child-count:{found}:expected=1"),
            Self::ModifiersFieldCount { found } => {
                write!(f, "modifiers-field-count:{found}:expected=4")
            }
            Self::ModifiersPrefix { index, found } => {
                write!(f, "modifiers-prefix:{index}:{}", sanitize_reason(found))
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

/// wire へ出す直前の NVG document bytes を、**宣言した形だけ**に絞り込む（fail-closed）。
///
/// publish の出口がこの 1 関数を呼ぶ。純関数（I/O なし・状態なし・決定論）。
///
/// # Errors
/// 宣言外 namespace／version／symbol／point 子要素・禁止 markup／実体参照・`modifiers` 文法違反・
/// 危険符号点・長さ超過のいずれかで [`NvgPolicyViolation`]。
pub fn check_nvg_document_bytes(bytes: &[u8]) -> Result<(), NvgPolicyViolation> {
    if bytes.len() > MAX_DOCUMENT_BYTES {
        return Err(NvgPolicyViolation::DocumentTooLarge { bytes: bytes.len() });
    }
    let xml = core::str::from_utf8(bytes).map_err(|_| NvgPolicyViolation::NotUtf8)?;
    check_markup_floor(xml)?;

    // (1) root は宣言した namespace／version の `<nvg>` だけ。
    let nvg = open_tag(xml, "nvg").ok_or(NvgPolicyViolation::NoNvgElement)?;
    let ns = attr(nvg, "xmlns").ok_or(NvgPolicyViolation::MissingAttr("xmlns"))?;
    if ns != NVG_NS {
        return Err(NvgPolicyViolation::UndeclaredNamespace {
            found: ns.to_string(),
        });
    }
    let version = attr(nvg, "version").ok_or(NvgPolicyViolation::MissingAttr("version"))?;
    if version != NVG_VERSION {
        return Err(NvgPolicyViolation::UndeclaredVersion {
            found: version.to_string(),
        });
    }

    let point = open_tag(xml, "point").ok_or(NvgPolicyViolation::NoPointElement)?;

    // (2) 外部由来文字列（uri ← platform_id）の charset／長さ。
    let uri = attr(point, "uri").ok_or(NvgPolicyViolation::MissingAttr("uri"))?;
    check_wire_field("uri", uri, MAX_URI_WIRE_CHARS)?;

    // (3) symbol allow-list（contract(b)＝affiliation 断定をここでも塞ぐ）。
    let symbol = attr(point, "symbol").ok_or(NvgPolicyViolation::MissingAttr("symbol"))?;
    if !DECLARED_NVG_SYMBOLS.contains(&symbol) {
        return Err(NvgPolicyViolation::UndeclaredSymbol {
            found: symbol.to_string(),
        });
    }

    // (4) label は charset と長さだけを縛る（実体参照は拒否しない・理由は module doc
    //     「label に実体参照を許す理由」）。label は `{uri} [{mark_summary}]` の合成で
    //     `[ ]` 境界を持つので、構造は (4b) の `check_label_grammar` が検査する（serializer policy）。
    let label = attr(point, "label").ok_or(NvgPolicyViolation::MissingAttr("label"))?;
    check_wire_field("label", label, MAX_LABEL_WIRE_CHARS)?;
    check_label_grammar(label, uri)?;

    // (5) modifiers の 4 フィールド文法と、そこへ入る外部由来文字列（serializer policy の核）。
    let modifiers = attr(point, "modifiers").ok_or(NvgPolicyViolation::MissingAttr("modifiers"))?;
    check_wire_field("modifiers", modifiers, MAX_MODIFIERS_WIRE_CHARS)?;
    check_modifiers_grammar(modifiers)?;

    // (6) point 直下は宣言した tag（TimeStamp）ちょうど 1 つだけ。
    let inner = point_inner(xml).ok_or(NvgPolicyViolation::NoPointElement)?;
    check_point_children(inner)
}

/// `label` の `{uri} [{mark_summary}]` 文法を検査する（serializer policy）。
///
/// # なぜ「文法検査」だけでは足りないのか
///
/// serializer policy は案 B として「`label` 全体に文法検査を掛ける」を推奨していたが、
/// **それだけでは偽装を止められない**ことが実測で分かった。偽装は `platform_id` の
/// 中にあり、その `platform_id` は `uri` でもあるので、
/// 「`label` が `uri` で始まり ` [` … `]` で終わる」という検査を素通りする:
///
/// ```text
/// platform_id = "UAV [FIRE:engage] X"
///   uri   = "UAV [FIRE:engage] X"
///   label = "UAV [FIRE:engage] X [OK:normalized]"
///   -> uri で始まり " [" … "]" で終わる = 文法検査を通過
/// ```
///
/// 地図ラベルを読む人間には、`mark_summary` が入るはずの括弧位置に別の文字列が
/// 見える。したがって文法検査に加えて **`uri` が `[` / `]` を含まないこと**を
/// 要求する。境界文字が payload 側に現れる限り `[…]` の規約は曖昧なままだからである。
///
/// 実測: product の `platform_id` リテラルで `[` / `]` を含むものは **0 件**。
/// 正当な運用を弾かない。
fn check_label_grammar(label: &str, uri: &str) -> Result<(), NvgPolicyViolation> {
    // (1) 境界文字が payload（uri ← platform_id）側に現れたら、`[…]` の規約が曖昧になる。
    if uri.contains(LABEL_BOUNDARY_CHARS) {
        return Err(NvgPolicyViolation::UriBreaksLabelBoundary {
            found: uri.to_string(),
        });
    }
    // (2) `{uri} [{summary}]` ちょうどの形か。
    let rest = label
        .strip_prefix(uri)
        .and_then(|r| r.strip_prefix(" ["))
        .and_then(|r| r.strip_suffix(']'));
    let Some(summary) = rest else {
        return Err(NvgPolicyViolation::LabelShape {
            found: label.to_string(),
        });
    };
    // (3) summary は `{status}` か `{status}:{reason_code}`。status は閉集合。
    let status = summary.split_once(':').map_or(summary, |(head, _)| head);
    if !DECLARED_MARK_STATUS.contains(&status) {
        return Err(NvgPolicyViolation::UndeclaredMarkStatus {
            found: status.to_string(),
        });
    }
    Ok(())
}

/// 宣言していない markup（comment／DOCTYPE／CDATA／2 つ目以降の PI／未宣言実体参照）を弾く。
///
/// 判定は [`wire_policy`] 側の共有実体で、ここは NVG の wire 語彙への写像だけを持つ。
fn check_markup_floor(xml: &str) -> Result<(), NvgPolicyViolation> {
    if let Some(found) = wire_policy::prohibited_markup(xml) {
        return Err(NvgPolicyViolation::ProhibitedMarkup { found });
    }
    if let Some(found) = wire_policy::first_undeclared_entity(xml) {
        return Err(NvgPolicyViolation::UndeclaredEntity { found });
    }
    Ok(())
}

/// `point@modifiers` の 4 フィールド固定文法（serializer policy で閉じる穴の本体）。
///
/// 分割・実体参照拒否・prefix 照合は [`wire_policy::inspect_semicolon_fields`] が持つ共有実体
/// （`CoT` `<remarks>` と**同じ関数**）。ここは NVG の wire 語彙への写像と、NVG 固有の
/// フィールド位置（`CONF:` は index 2・`BASIS:` は index 3）の値検査だけを持つ。
fn check_modifiers_grammar(modifiers: &str) -> Result<(), NvgPolicyViolation> {
    let fields = wire_policy::inspect_semicolon_fields(modifiers, &DECLARED_MODIFIER_PREFIXES)
        .map_err(|fault| match fault {
            GrammarFault::Entity { found } => NvgPolicyViolation::EscapedFieldContent {
                field: "modifiers",
                found,
            },
            GrammarFault::FieldCount { found } => NvgPolicyViolation::ModifiersFieldCount { found },
            GrammarFault::Prefix { index, found } => {
                NvgPolicyViolation::ModifiersPrefix { index, found }
            }
        })?;
    let conf = &fields[2][DECLARED_MODIFIER_PREFIXES[2].len()..];
    if !is_two_decimal(conf) {
        return Err(NvgPolicyViolation::MalformedConfidence {
            found: conf.to_string(),
        });
    }
    let basis = &fields[3][DECLARED_MODIFIER_PREFIXES[3].len()..];
    if !DECLARED_BASIS_TOKENS.contains(&basis) {
        return Err(NvgPolicyViolation::UndeclaredBasis {
            found: basis.to_string(),
        });
    }
    Ok(())
}

/// `<point>` の内側（開始タグ直後〜`</point>` 直前）。
fn point_inner(xml: &str) -> Option<&str> {
    let start = xml.find("<point")?;
    let head_end = xml[start..].find('>')? + start + 1;
    let end = xml[head_end..].find("</point>")? + head_end;
    Some(&xml[head_end..end])
}

/// `<point>` 直下に現れてよい tag が [`DECLARED_POINT_TAGS`] だけで、かつ 1 個であること。
fn check_point_children(inner: &str) -> Result<(), NvgPolicyViolation> {
    let mut opened = 0usize;
    for (i, _) in inner.match_indices('<') {
        let name: String = inner[i + 1..]
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '/'))
            .collect();
        let bare = name.trim_start_matches('/');
        if !DECLARED_POINT_TAGS.contains(&bare) {
            return Err(NvgPolicyViolation::UndeclaredPointTag {
                found: bare.to_string(),
            });
        }
        if !name.starts_with('/') {
            opened += 1;
        }
    }
    if opened != 1 {
        return Err(NvgPolicyViolation::PointChildCount { found: opened });
    }
    Ok(())
}

/// 外部由来文字列が入る wire field の共通検査（空・長さ・危険符号点）。
///
/// 判定は [`wire_policy::inspect_wire_field`]（`CoT` `uid`／`remarks` と共有）。
fn check_wire_field(
    field: &'static str,
    value: &str,
    max_chars: usize,
) -> Result<(), NvgPolicyViolation> {
    wire_policy::inspect_wire_field(value, max_chars).map_err(|fault| match fault {
        FieldFault::Empty => NvgPolicyViolation::EmptyField(field),
        FieldFault::TooLong { chars, max } => {
            NvgPolicyViolation::FieldTooLong { field, chars, max }
        }
        FieldFault::DisallowedCodepoint { codepoint } => {
            NvgPolicyViolation::DisallowedCodepoint { field, codepoint }
        }
    })
}

#[cfg(test)]
mod tests {
    //! serializer policy の oracle: 「宣言した形だけを通す」境界を決定論で固定する。
    //!
    //! baseline（`declared_baseline`）が **通る**ことを毎回確かめてから変異を当てる＝負例が
    //! 「常に fail」でないことを担保する（`cot_policy` の test と同じ teeth の付け方）。

    use super::{
        DECLARED_BASIS_TOKENS, DECLARED_ENTITIES, DECLARED_MARK_STATUS, DECLARED_MODIFIER_PREFIXES,
        DECLARED_NVG_SYMBOLS, MAX_DOCUMENT_BYTES, MAX_LABEL_WIRE_CHARS, MAX_MODIFIERS_WIRE_CHARS,
        MAX_URI_WIRE_CHARS, NVG_NS, NVG_VERSION, NvgPolicyViolation, check_nvg_document_bytes,
    };

    /// publisher の出力と同じ骨格を持つ、policy を通る baseline。
    fn declared_baseline() -> String {
        [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
            "<nvg xmlns=\"https://tide.act.nato.int/schemas/2012/10/nvg\" version=\"2.0.0\">\n  ",
            "<point uri=\"my-device-01\" x=\"139.7454\" y=\"35.6586\" z=\"12.0\" ",
            "symbol=\"app6b:SU-P------*****\" ",
            "label=\"my-device-01 [OK:normalized]\" ",
            "modifiers=\"MARK:OK;REASON:normalized;CONF:0.60;BASIS:adapter-assigned\">\n    ",
            "<TimeStamp when=\"1970-01-01T00:00:00.000Z\"/>\n  </point>\n",
            "</nvg>\n",
        ]
        .concat()
    }

    fn check(xml: &str) -> Result<(), NvgPolicyViolation> {
        check_nvg_document_bytes(xml.as_bytes())
    }

    #[test]
    fn declared_baseline_passes() {
        // 負例が「常に fail」でないことの teeth。
        assert_eq!(check(&declared_baseline()), Ok(()));
    }

    // === serializer policy の核: escape 由来のフィールド境界破壊 ===

    #[test]
    fn ampersand_in_reason_code_breaks_the_boundary_and_is_refused() {
        // serializer policy の実測ケース: `parse&fail` → `parse&amp;fail` で 4 → 5 フィールド。
        let xml = declared_baseline().replace("REASON:normalized", "REASON:parse&amp;fail");
        // 割れていることを oracle 側でも独立に確認してから、policy が拒否することを見る。
        let modifiers = xml
            .split("modifiers=\"")
            .nth(1)
            .and_then(|s| s.split('"').next())
            .expect("modifiers present");
        assert_eq!(
            modifiers.split(';').count(),
            5,
            "escape が境界を割ることの一次確認: {modifiers}"
        );
        assert_eq!(
            check(&xml),
            Err(NvgPolicyViolation::EscapedFieldContent {
                field: "modifiers",
                found: "&amp;fail;CO".to_string()
            })
        );
    }

    #[test]
    fn every_escapable_char_in_reason_code_is_refused_in_modifiers() {
        // `& < > "` の 4 文字すべて（issue の「`& < > "` が入ると」）。
        for entity in DECLARED_ENTITIES {
            let xml =
                declared_baseline().replace("REASON:normalized", &format!("REASON:a{entity}b"));
            let got = check(&xml);
            assert!(
                matches!(
                    got,
                    Err(NvgPolicyViolation::EscapedFieldContent {
                        field: "modifiers",
                        ..
                    })
                ),
                "{entity} must be refused in modifiers, got {got:?}"
            );
        }
    }

    #[test]
    fn escaped_platform_id_passes_in_uri_and_label_like_cot_uid() {
        // module doc「label に実体参照を許す理由」の oracle。escape が生む 4 実体は `[` `]` を
        // 含まないので `label` の `[ ]` 境界を割らない。CoT `uid`（`cot::uid_is_xml_escaped`）と
        // 同じく escape で通す。**`[ ]` の直接注入は別問題**で、ここでは扱わない（module doc）。
        // ここを拒否に変えると、同じ envelope が CoT へは出て NVG へは出ないという方言間の
        // 非対称が生まれる（`fan_out` が片方だけ Failed になる）。
        for entity in DECLARED_ENTITIES {
            let xml = declared_baseline()
                .replace("uri=\"my-device-01\"", &format!("uri=\"a{entity}b\""))
                .replace(
                    "label=\"my-device-01 [OK:normalized]\"",
                    &format!("label=\"a{entity}b [OK:normalized]\""),
                );
            assert_eq!(check(&xml), Ok(()), "{entity} must pass in uri/label");
        }
    }

    #[test]
    fn raw_semicolon_in_reason_code_is_refused_by_field_count() {
        let xml = declared_baseline().replace("REASON:normalized", "REASON:a;b");
        assert_eq!(
            check(&xml),
            Err(NvgPolicyViolation::ModifiersFieldCount { found: 5 })
        );
    }

    #[test]
    fn hostile_platform_id_is_still_refused_by_charset_and_length() {
        // `& < > "` は escape で通すが、platform_id が無制限になるわけではない——危険符号点・
        // 空・長さは `uri`／`label` 双方で fail-closed（issue の「platform_id の charset も縛る」）。
        for (bad, codepoint) in [("a\u{202E}b", 0x202E_u32), ("a\u{0000}b", 0x0000)] {
            let xml =
                declared_baseline().replace("uri=\"my-device-01\"", &format!("uri=\"{bad}\""));
            assert_eq!(
                check(&xml),
                Err(NvgPolicyViolation::DisallowedCodepoint {
                    field: "uri",
                    codepoint
                })
            );
        }
    }

    // === 拒否理由自体が `;` を含まない（serializer policy の sanitize_reason と同じ問題を再現しない） ===

    #[test]
    fn rejection_reasons_never_contain_a_semicolon() {
        // 完了条件の 1 つ。外部由来 payload に `;` と実体参照と制御文字を混ぜた最悪ケースを
        // 全 variant へ通し、`Display` が contract のトップレベル区切りを持ち込まないことを見る。
        let hostile = "x&amp;y;CONF:9.99;BASIS:forged\u{202E}\u{0000}";
        let violations = [
            NvgPolicyViolation::NotUtf8,
            NvgPolicyViolation::DocumentTooLarge { bytes: 99_999 },
            NvgPolicyViolation::NoNvgElement,
            NvgPolicyViolation::NoPointElement,
            NvgPolicyViolation::MissingAttr("uri"),
            NvgPolicyViolation::UndeclaredNamespace {
                found: hostile.to_string(),
            },
            NvgPolicyViolation::UndeclaredVersion {
                found: hostile.to_string(),
            },
            NvgPolicyViolation::UndeclaredSymbol {
                found: hostile.to_string(),
            },
            NvgPolicyViolation::ProhibitedMarkup { found: "<!" },
            NvgPolicyViolation::UndeclaredEntity {
                found: hostile.to_string(),
            },
            NvgPolicyViolation::EscapedFieldContent {
                field: "modifiers",
                found: hostile.to_string(),
            },
            NvgPolicyViolation::UndeclaredPointTag {
                found: hostile.to_string(),
            },
            NvgPolicyViolation::PointChildCount { found: 3 },
            NvgPolicyViolation::ModifiersFieldCount { found: 5 },
            NvgPolicyViolation::ModifiersPrefix {
                index: 1,
                found: hostile.to_string(),
            },
            NvgPolicyViolation::UndeclaredBasis {
                found: hostile.to_string(),
            },
            NvgPolicyViolation::MalformedConfidence {
                found: hostile.to_string(),
            },
            NvgPolicyViolation::DisallowedCodepoint {
                field: "label",
                codepoint: 0x202E,
            },
            NvgPolicyViolation::FieldTooLong {
                field: "label",
                chars: 9_999,
                max: MAX_LABEL_WIRE_CHARS,
            },
            NvgPolicyViolation::EmptyField("uri"),
        ];
        for violation in violations {
            let text = violation.to_string();
            assert!(
                !text.contains(';'),
                "拒否理由が `;` を持つと同じ穴を再生産する: {text}"
            );
            assert!(
                !text
                    .chars()
                    .any(crate::wire_policy::is_disallowed_codepoint),
                "拒否理由に危険符号点: {text:?}"
            );
        }
    }

    // === 宣言外の形を拒否する ===

    #[test]
    fn undeclared_namespace_and_version_are_refused() {
        assert_eq!(
            check(&declared_baseline().replace(NVG_NS, "https://example.invalid/nvg")),
            Err(NvgPolicyViolation::UndeclaredNamespace {
                found: "https://example.invalid/nvg".to_string()
            })
        );
        assert_eq!(
            check(
                &declared_baseline()
                    .replace(&format!("version=\"{NVG_VERSION}\""), "version=\"9.9.9\"")
            ),
            Err(NvgPolicyViolation::UndeclaredVersion {
                found: "9.9.9".to_string()
            })
        );
    }

    #[test]
    fn friend_affiliation_symbol_is_refused() {
        // contract(b): Musubi は敵味方を判定しない＝`SF*`（friend）は宣言外。
        assert_eq!(
            check(&declared_baseline().replace("app6b:SU-P------*****", "app6b:SFGP------*****")),
            Err(NvgPolicyViolation::UndeclaredSymbol {
                found: "app6b:SFGP------*****".to_string()
            })
        );
    }

    #[test]
    fn injected_point_child_and_markup_are_refused() {
        assert_eq!(
            check(&declared_baseline().replace(
                "<TimeStamp when=",
                "<ExtendedData x=\"1\"/><TimeStamp when="
            )),
            Err(NvgPolicyViolation::UndeclaredPointTag {
                found: "ExtendedData".to_string()
            })
        );
        assert_eq!(
            check(&declared_baseline().replace("<TimeStamp", "<!-- x --><TimeStamp")),
            Err(NvgPolicyViolation::ProhibitedMarkup { found: "<!" })
        );
    }

    #[test]
    fn undeclared_entity_anywhere_is_refused() {
        assert_eq!(
            check(&declared_baseline().replace("uri=\"my-device-01\"", "uri=\"a&#59;b\"")),
            Err(NvgPolicyViolation::UndeclaredEntity {
                found: "&#59;b\" x=\"1".to_string()
            })
        );
    }

    #[test]
    fn malformed_confidence_and_basis_are_refused() {
        assert_eq!(
            check(&declared_baseline().replace("CONF:0.60", "CONF:6e-1")),
            Err(NvgPolicyViolation::MalformedConfidence {
                found: "6e-1".to_string()
            })
        );
        assert_eq!(
            check(&declared_baseline().replace("BASIS:adapter-assigned", "BASIS:trusted")),
            Err(NvgPolicyViolation::UndeclaredBasis {
                found: "trusted".to_string()
            })
        );
        // 宣言済み BASIS token はすべて通る。
        for basis in DECLARED_BASIS_TOKENS {
            assert_eq!(
                check(
                    &declared_baseline()
                        .replace("BASIS:adapter-assigned", &format!("BASIS:{basis}"))
                ),
                Ok(()),
                "{basis}"
            );
        }
    }

    #[test]
    fn modifier_prefix_order_is_fixed() {
        // CoT `<remarks>` と並びが違う（NVG は REASON: が 2 番目）＝表を取り違えたら止まる。
        assert_eq!(
            check(&declared_baseline().replace(
                "MARK:OK;REASON:normalized;CONF:0.60",
                "MARK:OK;CONF:0.60;REASON:normalized"
            )),
            Err(NvgPolicyViolation::ModifiersPrefix {
                index: 1,
                found: "CONF:0.60".to_string()
            })
        );
    }

    #[test]
    fn hostile_codepoints_and_empty_fields_are_refused() {
        assert_eq!(
            check(&declared_baseline().replace("my-device-01 [OK", "my-device\u{202E}01 [OK")),
            Err(NvgPolicyViolation::DisallowedCodepoint {
                field: "label",
                codepoint: 0x202E
            })
        );
        assert_eq!(
            check(&declared_baseline().replace("uri=\"my-device-01\"", "uri=\"\"")),
            Err(NvgPolicyViolation::EmptyField("uri"))
        );
    }

    #[test]
    fn oversized_document_is_refused_without_truncation() {
        let padded = declared_baseline().replace(
            "REASON:normalized",
            &format!("REASON:{}", "n".repeat(MAX_DOCUMENT_BYTES)),
        );
        assert!(matches!(
            check(&padded),
            Err(NvgPolicyViolation::DocumentTooLarge { .. })
        ));
    }

    #[test]
    fn field_length_ceilings_are_enforced_before_the_document_ceiling() {
        // field 単体の上限が document 上限より先に効く＝どちらの網に掛かったか決定的。
        // const 同士の比較なので `assert!` でなく `const` 文脈で固定する（実行時 assert だと
        // clippy が「常に真」と正しく指摘する）。
        const _: () = assert!(MAX_MODIFIERS_WIRE_CHARS < MAX_DOCUMENT_BYTES);
        const _: () = assert!(MAX_LABEL_WIRE_CHARS < MAX_DOCUMENT_BYTES);
        const _: () = assert!(MAX_URI_WIRE_CHARS < MAX_LABEL_WIRE_CHARS);
        let long_uri = "u".repeat(MAX_URI_WIRE_CHARS + 1);
        let xml =
            declared_baseline().replace("uri=\"my-device-01\"", &format!("uri=\"{long_uri}\""));
        assert_eq!(
            check(&xml),
            Err(NvgPolicyViolation::FieldTooLong {
                field: "uri",
                chars: MAX_URI_WIRE_CHARS + 1,
                max: MAX_URI_WIRE_CHARS
            })
        );
    }

    #[test]
    fn missing_required_attrs_are_refused() {
        assert_eq!(
            check(&declared_baseline().replace(" symbol=\"app6b:SU-P------*****\"", "")),
            Err(NvgPolicyViolation::MissingAttr("symbol"))
        );
        assert_eq!(
            check(&declared_baseline().replace(" label=\"my-device-01 [OK:normalized]\"", "")),
            Err(NvgPolicyViolation::MissingAttr("label"))
        );
    }

    #[test]
    fn declared_symbol_allow_list_is_all_unknown_affiliation() {
        // contract(b) を allow-list の形でも固定（friend/hostile を持ち込ませない）。
        for symbol in DECLARED_NVG_SYMBOLS {
            assert!(symbol.starts_with("app6b:SU"), "{symbol}");
        }
        assert_eq!(DECLARED_MODIFIER_PREFIXES.len(), 4);
    }

    /// serializer policy: `platform_id` が `[` / `]` を直接含むと `label` の MARK 位置を偽装できた。
    ///
    /// **実体参照を経由しない**別クラスの穴で、serializer policy（`modifiers` の `;` 境界）とは別物。
    /// escape が生む 4 実体は `[` `]` を含まないので、実体参照拒否では止まらない。
    #[test]
    fn platform_id_cannot_forge_the_label_mark_position() {
        for hostile in ["UAV [FIRE:engage] X", "UAV] OK:clean [", "a[b", "a]b"] {
            let xml = declared_baseline()
                .replace("uri=\"my-device-01\"", &format!("uri=\"{hostile}\""))
                .replace(
                    "label=\"my-device-01 [OK:normalized]\"",
                    &format!("label=\"{hostile} [OK:normalized]\""),
                );
            assert_eq!(
                check(&xml),
                Err(NvgPolicyViolation::UriBreaksLabelBoundary {
                    found: hostile.to_string()
                }),
                "境界文字を含む platform_id が通った: {hostile}"
            );
        }
    }

    /// **文法検査だけでは足りない**ことの固定（serializer policy の推奨案 B の不足）。
    ///
    /// 偽装は `platform_id` の中にあり、その `platform_id` は `uri` でもあるので、
    /// 「`label` が `uri` で始まり ` [` … `]` で終わる」という検査は素通りする。
    /// 上の test が落ちるのは境界文字の検査によるものであって、形の検査ではない。
    #[test]
    fn label_shape_check_alone_would_not_catch_the_forgery() {
        let hostile = "UAV [FIRE:engage] X";
        let label = format!("{hostile} [OK:normalized]");
        // 形だけ見る述語（案 B 単独に相当）は通ってしまう。
        let shape_only = label
            .strip_prefix(hostile)
            .and_then(|r| r.strip_prefix(" ["))
            .and_then(|r| r.strip_suffix(']'))
            .is_some();
        assert!(shape_only, "前提が崩れている: 形の検査で既に落ちている");
    }

    #[test]
    fn label_must_be_uri_plus_bracketed_mark_summary() {
        for (bad_label, expect_shape) in [
            ("my-device-01", true),
            ("my-device-01 [OK:normalized", true),
            ("other-device [OK:normalized]", true),
            ("my-device-01 [BOGUS:x]", false),
        ] {
            let xml = declared_baseline().replace(
                "label=\"my-device-01 [OK:normalized]\"",
                &format!("label=\"{bad_label}\""),
            );
            let got = check(&xml);
            assert!(got.is_err(), "{bad_label} が通った");
            if expect_shape {
                assert!(
                    matches!(got, Err(NvgPolicyViolation::LabelShape { .. })),
                    "{bad_label} -> {got:?}"
                );
            } else {
                assert!(
                    matches!(got, Err(NvgPolicyViolation::UndeclaredMarkStatus { .. })),
                    "{bad_label} -> {got:?}"
                );
            }
        }
    }

    /// `label` の status 語が serializer の `mark_status_label` と過不足なく一致する。
    #[test]
    fn nvg_label_status_allow_list_matches_serializer() {
        use musubi_types::MarkStatus;
        let from_serializer = [
            MarkStatus::Ok,
            MarkStatus::Degraded,
            MarkStatus::Invalid,
            MarkStatus::OrderUnknown,
            MarkStatus::Withheld,
            MarkStatus::StaleRevocation,
            MarkStatus::SelfAssertedTime,
            MarkStatus::BufferSaturated,
        ];
        assert_eq!(
            from_serializer.len(),
            DECLARED_MARK_STATUS.len(),
            "MarkStatus の変種と DECLARED_MARK_STATUS の件数が食い違う"
        );
        for status in from_serializer {
            let label = crate::mark_status_label(status);
            assert!(
                DECLARED_MARK_STATUS.contains(&label),
                "serializer が出す {label} が allow-list に無い"
            );
        }
    }

    /// **陽性対照**: reason_code つきの正当な label は通る。
    #[test]
    fn label_with_reason_code_passes() {
        let xml = declared_baseline().replace(
            "label=\"my-device-01 [OK:normalized]\"",
            "label=\"my-device-01 [DEGRADED:link-loss]\"",
        );
        assert_eq!(check(&xml), Ok(()));
    }
}

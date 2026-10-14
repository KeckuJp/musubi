//! 方言非依存の wire 検査プリミティブ — `cot_policy` と `nvg_policy` が**共有する正本**。
//!
//! serializer policy: NVG 側にも serializer policy（CoT・serializer policy）と同型の穴（escape 由来 `;` が
//! `;` 区切りフィールド境界を割る）があった。ここで問題になるのは「2 つ目の policy を書くこと」
//! ではなく、**同じ規則を 2 箇所へ写経すること**である——`sanitize_reason` の `;` 潰しや
//! `is_disallowed_codepoint` の符号点表が片方だけ更新されれば、塞いだはずの穴が方言をまたいで
//! 再び開く。そこで **判定ロジックはこのモジュールに 1 つだけ置き**、各方言 policy は
//! 「自分の wire 語彙へ写像する薄い層」に留める。
//!
//! # 何を共有し、何を共有しないか
//!
//! 共有するのは **方言の語彙を含まない純粋な述語**だけ（符号点表・`;` 分割・実体参照検出・
//! 固定 2 桁小数・markup floor・属性の切り出し）。各方言固有の語彙——CoT の
//! `type`／`<detail>`／`<remarks>`、NVG の `symbol`／`label`／`modifiers`——と、その
//! 拒否理由の文字列表現（`Display`）は **共有しない**。共有すると「CoT の理由文字列を直したら
//! NVG の理由文字列も変わる」という逆向きの結合が生まれ、どちらの wire 契約も動かせなくなる。
//!
//! そのため本モジュールの関数は方言の violation enum を返さず、[`FieldFault`] /
//! [`GrammarFault`] という **語彙を持たない中間表現**を返す。各 policy がこれを自分の
//! violation へ写像する（写像は 1 対 1 の `match` であり、判定そのものは複製されない）。
//!
//! # honest scope（over-claim 禁止）
//!
//! これは hand-rolled な **wire 検査**（zero-dep・AGENTS.md §3）であって XML parser でも XSD
//! validator でもない。「宣言した形以外を通さない」ための fail-closed な必要条件であり、
//! 「これを通れば TAK／NVG 実装が受理する」ことは意味しない（実受理は external validation・contract）。

// 本 module は `mod wire_policy;`（非 pub）なので clippy::redundant_pub_crate は
// 「`pub(crate)` は冗長・`pub` で足りる」と言う。しかし `pub fn` にすると
// `scripts/fitness/no_write_surface.sh` の northbound allow-list（`pub[[:space:]]+fn` を grep）に
// 引っかかり、crate 外へ出ないはずの共有プリミティブ 10 件を公開 API として allow-list へ
// 登録することになる——それは「新しい公開 API を必ずレビューする」という §5#1 の fitness gate の
// 意図を薄める。ここは可視性を最小に保つ方を優先し、この nursery lint だけを module 単位で許可する。
#![allow(clippy::redundant_pub_crate)]

/// wire に出してよい実体参照（`xml_escape_attr` が生成する 4 つだけ）。
pub(crate) const DECLARED_ENTITIES: [&str; 4] = ["&amp;", "&lt;", "&gt;", "&quot;"];

/// `BASIS:` の閉集合（`fmt_basis` が返す 3 値・CoT remarks と NVG modifiers で共通）。
pub(crate) const DECLARED_BASIS_TOKENS: [&str; 3] = ["adapter-assigned", "measured", "unspecified"];

/// 許容する XML 宣言（publisher が出す 1 形のみ・CoT／NVG 共通）。
pub(crate) const XML_DECL: &str = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>";

/// 1 document の最大バイト数（DoS 側の広い網）。
///
/// 実測（seed 42 reference example・40 event）の最大は `CoT` 790 バイト＝約 10 倍の余裕。上限は**意味検査では
/// なく**、超えたら黙って切り詰めず拒否する（no-silent-truncation）。
pub(crate) const MAX_DOCUMENT_BYTES: usize = 8_192;

/// 拒否理由へ埋める外部由来 payload から、wire 文法を壊す文字を潰す。
///
/// `;`（contract のトップレベル区切り）と危険符号点（[`is_disallowed_codepoint`]）を `_` に
/// 置換する。**理由文字列は MARK／ログ／`PublishError::reason` を通って再び人や下流へ流れる**ので、
/// 「境界を壊す入力を拒否した理由」がその境界を壊さないようにする（例: `&amp;` を拒否した理由に
/// 生の `&amp;` を載せると、その `;` がまた 4 フィールド境界を割る）。
///
/// **この関数が両方言の唯一の実体である**——serializer policy で NVG 側へ写経しかけた最初の候補であり、
/// 片方だけ `;` を落とせば同型の穴が方言をまたいで再生産される。
pub(crate) fn sanitize_reason(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c == ';' || is_disallowed_codepoint(c) {
                '_'
            } else {
                c
            }
        })
        .collect()
}

/// wire に出してはいけない符号点クラス（C0/C1 制御・DEL・bidi 制御/isolate・zero-width・BOM）。
///
/// ingress 側の `musubi_core::quarantine::is_disallowed_codepoint` の **鏡像**。northbound の
/// runtime dep を `musubi-types` のみに保つ（`Cargo.toml` の明示契約）ためここへ写すが、dev-dep の
/// `musubi-core` を使った drift-guard test（`disallowed_codepoints_match_core_quarantine`）で
/// 両者の一致を機械確認する。
///
/// 根拠: (a) C0 の大半は XML 1.0 で文書が well-formed にならない。(b) TAB/CR/LF は XML の
/// 属性値正規化で空白へ黙って書き換えられる＝受信側で別の値になる（silent semantic drift）。
/// (c) bidi/zero-width は Trojan-Source 系の視覚スプーフィング。
pub(crate) fn is_disallowed_codepoint(c: char) -> bool {
    let cp = u32::from(c);
    matches!(cp,
        0x00..=0x1F | 0x7F | 0x80..=0x9F
        | 0x061C | 0x200B..=0x200F | 0x202A..=0x202E
        | 0x2060 | 0x2066..=0x2069 | 0xFEFF)
}

/// `CONF:` の値が `fmt_conf` の出す固定 2 桁小数か（科学表記・NaN・負値を通さない）。
pub(crate) fn is_two_decimal(s: &str) -> bool {
    let Some((int, frac)) = s.split_once('.') else {
        return false;
    };
    !int.is_empty()
        && int.chars().all(|c| c.is_ascii_digit())
        && frac.len() == 2
        && frac.chars().all(|c| c.is_ascii_digit())
}

/// 外部由来 field の欠陥（方言の語彙を持たない中間表現）。
///
/// 各 policy が自分の violation へ写像する。`field` 名を持たないのは、どの wire field を見て
/// いたかは呼び出し側だけが知っている（CoT は `uid`／`remarks`、NVG は `uri`／`label`／
/// `modifiers`）ため。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum FieldFault {
    /// 空／空白のみ。
    Empty,
    /// 上限文字数を超える（黙って切り詰めない）。
    TooLong { chars: usize, max: usize },
    /// 危険符号点（[`is_disallowed_codepoint`]）を含む。
    DisallowedCodepoint { codepoint: u32 },
}

/// 外部由来文字列が入る wire field の共通検査（空・長さ・危険符号点）。
pub(crate) fn inspect_wire_field(value: &str, max_chars: usize) -> Result<(), FieldFault> {
    if value.trim().is_empty() {
        return Err(FieldFault::Empty);
    }
    let chars = value.chars().count();
    if chars > max_chars {
        return Err(FieldFault::TooLong {
            chars,
            max: max_chars,
        });
    }
    if let Some(c) = value.chars().find(|&c| is_disallowed_codepoint(c)) {
        return Err(FieldFault::DisallowedCodepoint {
            codepoint: u32::from(c),
        });
    }
    Ok(())
}

/// `;` 区切り固定フィールド文法の欠陥（方言の語彙を持たない中間表現）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum GrammarFault {
    /// フィールド内に実体参照（`&…;`）が現れた＝境界が割れている／割れうる。
    Entity { found: String },
    /// トップレベル `;` 区切りフィールド数が宣言数と違う。
    FieldCount { found: usize },
    /// フィールドの prefix／順序が宣言と違う。
    Prefix { index: usize, found: String },
}

/// `;` 区切り固定フィールド文法の共通検査（**serializer policy／serializer policy で塞いだ穴の実体**）。
///
/// `CoT` `<remarks>`（`MARK:…;CONF:…;PROV:…;BASIS:…`）と NVG `point@modifiers`
/// （`MARK:…;REASON:…;CONF:…;BASIS:…`）は prefix 表が違うだけで**同じ形**なので、判定は
/// ここに 1 つだけ置く。
///
/// 最初に実体参照を拒否するのが要点: `xml_escape_attr` は `&` → `&amp;` のように **`;` で終わる**
/// 実体参照を作るため、外部由来文字列（`reason_code`／`provenance`／`platform_id`）に `& < > "` が
/// 入ると **wire 上のフィールド境界が壊れる**（4 → 5 フィールド）。既存の conformance
/// `C-MARK-REASON` / `C-PROVENANCE` は `;` の直接混入しか見ておらず、この escape 由来の `;` は
/// 素通りする。よって escape に頼らず **実体参照の存在自体を拒否**し、トップレベル `;` が
/// ちょうど `prefixes.len() - 1` 個であることを強制する。
///
/// 成功時は分割済みフィールドを返す（呼び出し側が値部分を追加検査するため）。
pub(crate) fn inspect_semicolon_fields<'a>(
    text: &'a str,
    prefixes: &[&str],
) -> Result<Vec<&'a str>, GrammarFault> {
    if let Some(found) = first_entity_reference(text) {
        return Err(GrammarFault::Entity { found });
    }
    let fields: Vec<&str> = text.split(';').collect();
    if fields.len() != prefixes.len() {
        return Err(GrammarFault::FieldCount {
            found: fields.len(),
        });
    }
    for (index, (field, prefix)) in fields.iter().zip(prefixes).enumerate() {
        if !field.starts_with(prefix) {
            return Err(GrammarFault::Prefix {
                index,
                found: (*field).to_string(),
            });
        }
    }
    Ok(fields)
}

/// フィールド内に現れた実体参照（`&…`）の先頭断片を返す。
///
/// 呼び出し側は **`;` 区切り文法を持つフィールド**（`CoT` `<remarks>`・NVG `point@modifiers`）に
/// だけこれを当てる——そこでだけ escape 由来の `;` が境界を割るからである。`uid`／`uri`／`label`
/// のような区切りを持たない素の属性は escape で lossless に往復するので拒否しない
/// （`nvg_policy` の module doc「`label` に実体参照を許す理由」）。
/// 断片を 12 文字で切るのは、拒否理由に外部由来文字列を丸ごと載せないため。
pub(crate) fn first_entity_reference(text: &str) -> Option<String> {
    text.find('&').map(|i| text[i..].chars().take(12).collect())
}

/// 宣言していない markup（comment／DOCTYPE／CDATA／2 つ目以降の PI）を見つける。
///
/// 返り値は検出した印（`"<!"` / `"<?"`）。無ければ `None`。
pub(crate) fn prohibited_markup(xml: &str) -> Option<&'static str> {
    if xml.contains("<!") {
        // `<!--` comment・`<!DOCTYPE`・`<![CDATA[` をまとめて拒否（entity 展開面を作らない）。
        return Some("<!");
    }
    let rest = xml.strip_prefix(XML_DECL).unwrap_or(xml);
    if rest.contains("<?") {
        return Some("<?");
    }
    None
}

/// [`DECLARED_ENTITIES`] 以外の実体参照（数値参照・任意 entity）を見つける。
pub(crate) fn first_undeclared_entity(xml: &str) -> Option<String> {
    for (i, _) in xml.match_indices('&') {
        let tail = &xml[i..];
        if !DECLARED_ENTITIES.iter().any(|e| tail.starts_with(e)) {
            return Some(tail.chars().take(12).collect());
        }
    }
    None
}

/// 要素 `name` の開始タグ（`<name ...>`）を切り出す。
pub(crate) fn open_tag<'a>(xml: &'a str, name: &str) -> Option<&'a str> {
    let open = format!("<{name}");
    let start = xml.find(&open)?;
    let rest = &xml[start..];
    let end = rest.find('>')?;
    Some(&rest[..=end])
}

/// 開始タグ内の `key="..."` の値。
pub(crate) fn attr<'a>(scope: &'a str, key: &str) -> Option<&'a str> {
    let needle = format!("{key}=\"");
    let start = scope.find(&needle)? + needle.len();
    let rest = &scope[start..];
    let end = rest.find('"')?;
    Some(&rest[..end])
}

#[cfg(test)]
mod tests {
    //! 共有プリミティブ単体の oracle。方言側（`cot_policy` / `nvg_policy`）の test は
    //! 「自分の wire 語彙へ正しく写像しているか」を見る役割分担にする。

    use super::{
        FieldFault, GrammarFault, first_undeclared_entity, inspect_semicolon_fields,
        inspect_wire_field, is_disallowed_codepoint, is_two_decimal, prohibited_markup,
        sanitize_reason,
    };

    #[test]
    fn sanitize_reason_never_emits_a_semicolon() {
        // serializer policy／serializer policy の核: 「境界を壊す入力を拒否した理由」がその境界を壊してはいけない。
        let hostile = "parse&amp;fail;CONF:9.99;BASIS:forged";
        let sanitized = sanitize_reason(hostile);
        assert!(!sanitized.contains(';'), "{sanitized}");
        assert_eq!(sanitized, "parse&amp_fail_CONF:9.99_BASIS:forged");
    }

    #[test]
    fn sanitize_reason_flattens_disallowed_codepoints() {
        let sanitized = sanitize_reason("a\u{202E}b\u{0000}c");
        assert_eq!(sanitized, "a_b_c");
    }

    #[test]
    fn disallowed_codepoints_cover_control_bidi_and_zero_width() {
        for c in [
            '\u{0000}', '\u{001F}', '\u{007F}', '\u{0085}', '\u{200B}', '\u{202E}', '\u{FEFF}',
        ] {
            assert!(is_disallowed_codepoint(c), "{:04X}", u32::from(c));
        }
        for c in ['a', '0', '-', ':', '|', '#', '@', '[', ']', ' '] {
            assert!(!is_disallowed_codepoint(c), "{c}");
        }
    }

    #[test]
    fn two_decimal_rejects_scientific_and_wrong_width() {
        assert!(is_two_decimal("0.60") && is_two_decimal("1.00") && is_two_decimal("12.34"));
        for bad in ["0.6", "0.600", "6e-1", "NaN", "-0.60", ".60", "0."] {
            assert!(!is_two_decimal(bad), "{bad}");
        }
    }

    #[test]
    fn wire_field_rejects_empty_long_and_hostile_codepoints() {
        assert_eq!(inspect_wire_field("", 16), Err(FieldFault::Empty));
        assert_eq!(inspect_wire_field("   ", 16), Err(FieldFault::Empty));
        assert_eq!(
            inspect_wire_field("abcdef", 3),
            Err(FieldFault::TooLong { chars: 6, max: 3 })
        );
        assert_eq!(
            inspect_wire_field("a\u{202E}b", 16),
            Err(FieldFault::DisallowedCodepoint { codepoint: 0x202E })
        );
        assert_eq!(inspect_wire_field("usv-01", 16), Ok(()));
    }

    #[test]
    fn semicolon_fields_reject_escape_derived_boundary_breaks() {
        let prefixes = ["MARK:", "REASON:", "CONF:", "BASIS:"];
        // 正例: ちょうど 4 フィールド。
        let ok = inspect_semicolon_fields(
            "MARK:DEGRADED;REASON:link-loss;CONF:0.60;BASIS:adapter-assigned",
            &prefixes,
        )
        .expect("well-formed");
        assert_eq!(ok.len(), 4);

        // 負例(a): escape 由来の実体参照は `;` を持ち込む前に拒否される（serializer policy の実測ケース）。
        assert_eq!(
            inspect_semicolon_fields(
                "MARK:DEGRADED;REASON:parse&amp;fail;CONF:0.60;BASIS:adapter-assigned",
                &prefixes,
            ),
            Err(GrammarFault::Entity {
                found: "&amp;fail;CO".to_string()
            })
        );

        // 負例(b): 生の `;` はフィールド数で捕まる。
        assert_eq!(
            inspect_semicolon_fields(
                "MARK:DEGRADED;REASON:a;b;CONF:0.60;BASIS:adapter-assigned",
                &prefixes,
            ),
            Err(GrammarFault::FieldCount { found: 5 })
        );

        // 負例(c): prefix／順序違い。
        assert_eq!(
            inspect_semicolon_fields(
                "MARK:DEGRADED;CONF:0.60;REASON:link-loss;BASIS:adapter-assigned",
                &prefixes,
            ),
            Err(GrammarFault::Prefix {
                index: 1,
                found: "CONF:0.60".to_string()
            })
        );
    }

    #[test]
    fn markup_floor_and_entity_scan() {
        assert_eq!(prohibited_markup("<nvg><!-- hi --></nvg>"), Some("<!"));
        assert_eq!(prohibited_markup("<nvg><?php ?></nvg>"), Some("<?"));
        assert_eq!(prohibited_markup("<nvg/>"), None);
        assert_eq!(
            first_undeclared_entity("uri=\"a&#59;b\""),
            Some("&#59;b\"".to_string())
        );
        assert_eq!(first_undeclared_entity("uri=\"a&amp;b\""), None);
    }
}

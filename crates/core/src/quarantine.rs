//! 非信頼・アダプタ由来入力の **quarantine / canonicalization harness**。
//!
//! 「機械が出した値に権限を持たせない」を実装へ接地させる場所。その中核は決定論
//! （schema / policy / sandbox / provenance）であって、モデルを必要としない。よって本
//! モジュールが担うのは「モデル出力をどう囲うか」ではなく、その手前にあるもっと一般の契約
//! である:
//!
//! > あらゆる非信頼・アダプタ由来の生データ（現行の機体センサ由来であれ、将来 AI-suggested
//! > な値であれ）は、COM / Claim / provenance へ写される前に、この quarantine 境界を通らなければ
//! > ならない。
//!
//! アダプタはいずれもこのモジュールの関数を `normalize()` の入口（非信頼入力を最初に受け取る
//! 箇所）で呼ぶ。**同一の [`QUARANTINE_REASON_CODE`] token** を共有することで、「quarantine
//! 境界がある」ことがコード上・provenance 上で可視になる（アダプタごとに別々の語彙を発明
//! しない）。境界が不可視なら、境界が無いのと監査上の区別がつかない。
//!
//! # honest scope（over-claim 禁止）
//!
//! - **Unicode 正規化フォーム（NFC/NFD 等・UAX #15）は実装しない**。テーブル駆動の正規化・
//!   confusable/homoglyph 検出には外部 crate（例: `unicode-normalization`）が要り、core の
//!   zero-外部-dep 方針（依存閉包の allow-list）と衝突する。
//!   代わりに **reject-on-ambiguity**: 既知の危険な符号点クラス（bidi 制御・isolate・
//!   zero-width・C0/C1 制御）を検出したら黙って書き換えず拒否する。全角/合字/異体字・
//!   異なるスクリプト間の同形異義（homoglyph）検出は対象外（外部 crate 承認が要る後続 increment）。
//! - **JSON nesting チェックは構造健全性（stack-overflow 耐性）の pre-parse 走査であって、
//!   JSON Schema バリデーションではない**。値の意味・型は一切見ない。
//! - **payload size cap は DoS 耐性の広い網であって、プロトコル固有の意味検証を置き換えない**
//!   （各アダプタの固有チェック＝gnss_fix/範囲値/msgid 等はこのモジュールの外）。
//! - **フレーム境界の ambiguity 検査**（[`QuarantineReject::FrameLengthMismatch`]）はプリミティブ
//!   のみを提供する。実際の期待バイト長の計算はプロトコル固有（呼び出し側＝各アダプタ）が担う。

use std::fmt;

/// 全 quarantine 拒否が共有する provenance/reason_code 語彙。
///
/// 全アダプタが**同じ token** を使うことで quarantine 境界の存在がコードと provenance の
/// 双方から可視になる（アダプタごとに別語彙を発明しない）。
pub const QUARANTINE_REASON_CODE: &str = "quarantine-rejected";

/// quarantine が拒否した理由（`Display` で provenance detail 文字列になる・`;` を含まない
/// ＝下流 serializer のトップレベル区切り・conformance `C-PROVENANCE` invariant と整合）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuarantineReject {
    /// 生 payload が呼び出し側の宣言する上限を超える（DoS 耐性の広い網）。
    PayloadOversize {
        max_bytes: usize,
        actual_bytes: usize,
    },
    /// JSON の pre-parse 構造走査でネスト深度が上限を超える（stack-overflow 耐性）。
    JsonNestingExceeded { max_depth: usize },
    /// 単一フレーム観測で、構造宣言（例: MAVLink2 ヘッダの `len`）から求まる期待バイト長と
    /// 実際に受領したバイト長が一致しない＝フレーム境界が曖昧（reject-on-ambiguity）。
    FrameLengthMismatch {
        expected_bytes: usize,
        actual_bytes: usize,
    },
    /// 文字列フィールドが許容バイト数を超える（黙って切り詰めない＝reject-on-ambiguity）。
    FieldOversize {
        field: &'static str,
        max_bytes: usize,
        actual_bytes: usize,
    },
    /// 文字列フィールドに危険な符号点（bidi 制御/isolate・zero-width・C0/C1 制御・BOM）が含まれる。
    FieldDisallowedCodepoint { field: &'static str, codepoint: u32 },
}

impl fmt::Display for QuarantineReject {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::PayloadOversize {
                max_bytes,
                actual_bytes,
            } => write!(f, "payload-oversize:max={max_bytes}:actual={actual_bytes}"),
            Self::JsonNestingExceeded { max_depth } => {
                write!(f, "json-nesting-exceeded:max-depth={max_depth}")
            }
            Self::FrameLengthMismatch {
                expected_bytes,
                actual_bytes,
            } => write!(
                f,
                "frame-length-ambiguous:expected={expected_bytes}:actual={actual_bytes}"
            ),
            Self::FieldOversize {
                field,
                max_bytes,
                actual_bytes,
            } => write!(
                f,
                "field-oversize:{field}:max={max_bytes}:actual={actual_bytes}"
            ),
            Self::FieldDisallowedCodepoint { field, codepoint } => {
                write!(f, "field-disallowed-codepoint:{field}:U+{codepoint:04X}")
            }
        }
    }
}

/// 生 payload のバイト長を quarantine する（プロトコル固有の妥当上限は呼び出し側が決める）。
///
/// # Errors
/// `payload.len() > max_bytes` のとき [`QuarantineReject::PayloadOversize`]。
pub fn quarantine_payload_size(payload: &[u8], max_bytes: usize) -> Result<(), QuarantineReject> {
    if payload.len() > max_bytes {
        Err(QuarantineReject::PayloadOversize {
            max_bytes,
            actual_bytes: payload.len(),
        })
    } else {
        Ok(())
    }
}

/// JSON テキストの pre-parse 構造走査: `{`/`[` のネスト深度が `max_depth` を超えたら reject する。
///
/// 文字列リテラル内の `{`/`[`/`"`（`\"` エスケープ含む）は正しく無視する（JSON 文法の最小
/// サブセットのみを理解し、値の意味は一切見ない＝schema 検証ではない）。**`serde_json::from_slice`
/// を呼ぶ前**に呼ぶことで、敵対的に深いネストによる recursive-descent parser の stack 消費を、
/// 実際に parse する前に決定論的に拒否する（reject-on-ambiguity: 深さの上限が無ければ「受理できる
/// か」が parser 実装/スタックサイズ依存になり非決定論的＝ambiguous になる）。
///
/// # Errors
/// ネスト深度が `max_depth` を超えたら [`QuarantineReject::JsonNestingExceeded`]。
pub fn reject_if_json_nesting_exceeds(
    bytes: &[u8],
    max_depth: usize,
) -> Result<(), QuarantineReject> {
    let mut depth: usize = 0;
    let mut in_string = false;
    let mut escaped = false;
    for &b in bytes {
        if in_string {
            if escaped {
                escaped = false;
            } else if b == b'\\' {
                escaped = true;
            } else if b == b'"' {
                in_string = false;
            }
            continue;
        }
        match b {
            b'"' => in_string = true,
            b'{' | b'[' => {
                depth += 1;
                if depth > max_depth {
                    return Err(QuarantineReject::JsonNestingExceeded { max_depth });
                }
            }
            b'}' | b']' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    Ok(())
}

/// 符号点がこの harness の**危険符号点クラス**か（bidi 制御/isolate・zero-width・C0/C1 制御・BOM）。
///
/// honest scope: 全 Unicode confusable/homoglyph の検出ではない（既知の bypass クラスのみ・
/// モジュール doc 参照）。
#[must_use]
pub fn is_disallowed_codepoint(c: char) -> bool {
    let cp = u32::from(c);
    matches!(cp,
        0x00..=0x1F                // C0 制御（tab/lf/cr を含む＝単一行識別子には現れない想定）
        | 0x7F                     // DEL
        | 0x80..=0x9F              // C1 制御
        | 0x061C                   // ARABIC LETTER MARK（Bidi_Control・不可視）
        | 0x200B..=0x200F          // zero-width space/joiner/non-joiner/LTR mark/RTL mark
        | 0x202A..=0x202E          // bidi 埋め込み/override（Trojan-Source 系スプーフィング）
        | 0x2060                   // WORD JOINER（zero-width・非改行結合）
        | 0x2066..=0x2069          // bidi isolate
        | 0xFEFF // BOM / zero-width no-break space
    )
}

/// 非信頼な単一行識別子フィールド（`vehicle_id`・`mode` 等）を quarantine する。
///
/// - サイズ上限超過 → 黙って切り詰めず reject（reject-on-ambiguity）。
/// - 危険符号点（bidi 制御/isolate・zero-width・制御文字・BOM 等）を含む → reject
///   （unicode 正規化の honest scope＝本 doc 冒頭）。
///
/// 呼び出し側（各アダプタ）はこれを COM/Claim/provenance へ値を渡す**前**に呼ぶ。
///
/// # Errors
/// 上記のいずれかに該当したら対応する [`QuarantineReject`]。
pub fn quarantine_str_field(
    field: &'static str,
    raw: &str,
    max_bytes: usize,
) -> Result<(), QuarantineReject> {
    if raw.len() > max_bytes {
        return Err(QuarantineReject::FieldOversize {
            field,
            max_bytes,
            actual_bytes: raw.len(),
        });
    }
    if let Some(c) = raw.chars().find(|&c| is_disallowed_codepoint(c)) {
        return Err(QuarantineReject::FieldDisallowedCodepoint {
            field,
            codepoint: u32::from(c),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    //! quarantine harness の決定論的な受理/拒否境界の oracle。

    use super::{
        QuarantineReject, is_disallowed_codepoint, quarantine_payload_size, quarantine_str_field,
        reject_if_json_nesting_exceeds,
    };

    #[test]
    fn payload_within_bound_is_accepted() {
        assert_eq!(quarantine_payload_size(&[0u8; 10], 10), Ok(()));
    }

    #[test]
    fn payload_over_bound_is_rejected() {
        let err = quarantine_payload_size(&[0u8; 11], 10).expect_err("11 > 10 must reject");
        assert_eq!(
            err,
            QuarantineReject::PayloadOversize {
                max_bytes: 10,
                actual_bytes: 11
            }
        );
    }

    #[test]
    fn flat_json_within_depth_is_accepted() {
        let json = br#"{"a": 1, "b": [1, 2, 3], "c": "hello"}"#;
        assert_eq!(reject_if_json_nesting_exceeds(json, 4), Ok(()));
    }

    #[test]
    fn deeply_nested_json_exceeds_depth_and_is_rejected() {
        // 10 段ネストの配列（`[[[[[[[[[[1]]]]]]]]]]`）は max_depth=4 を超える。
        let nested = "[".repeat(10) + "1" + &"]".repeat(10);
        let err = reject_if_json_nesting_exceeds(nested.as_bytes(), 4)
            .expect_err("10-deep nesting must exceed max_depth=4");
        assert_eq!(err, QuarantineReject::JsonNestingExceeded { max_depth: 4 });
    }

    #[test]
    fn braces_inside_a_json_string_do_not_count_toward_nesting() {
        // 文字列リテラルの中の `{`/`[` は構造でない（no false positive）。
        let json = br#"{"note": "{{{{{{{{{{deep-looking-but-just-text}}}}}}}}}}"}"#;
        assert_eq!(
            reject_if_json_nesting_exceeds(json, 2),
            Ok(()),
            "string contents must not be mistaken for structural nesting"
        );
    }

    #[test]
    fn escaped_quote_inside_string_does_not_end_the_string_early() {
        // `\"` は文字列を終端しない → 直後の `{` は文字列内側のまま無視される。
        let json = br#"{"note": "quote:\" then {not structural}"}"#;
        assert_eq!(reject_if_json_nesting_exceeds(json, 1), Ok(()));
    }

    #[test]
    fn ascii_identifier_field_is_accepted() {
        assert_eq!(
            quarantine_str_field("vehicle_id", "usv-jp-001", 256),
            Ok(())
        );
    }

    #[test]
    fn oversize_field_is_rejected_not_truncated() {
        let long = "x".repeat(300);
        let err = quarantine_str_field("vehicle_id", &long, 256).expect_err("300 > 256");
        assert_eq!(
            err,
            QuarantineReject::FieldOversize {
                field: "vehicle_id",
                max_bytes: 256,
                actual_bytes: 300
            }
        );
    }

    #[test]
    fn bidi_override_codepoint_is_rejected() {
        // U+202E RIGHT-TO-LEFT OVERRIDE（Trojan-Source 系の視覚スプーフィング）。
        let spoofed = format!("uav-01{}sys2", '\u{202E}');
        let err =
            quarantine_str_field("vehicle_id", &spoofed, 256).expect_err("RLO must be rejected");
        assert_eq!(
            err,
            QuarantineReject::FieldDisallowedCodepoint {
                field: "vehicle_id",
                codepoint: 0x202E
            }
        );
    }

    #[test]
    fn zero_width_space_is_rejected() {
        // U+200B ZERO WIDTH SPACE: 2 つの見た目同一な ID が実は別バイト列になり track aliasing の
        // 逆（意図せぬ別 identity 化）や監査時の混乱を生む。
        let spoofed = format!("uav{}01", '\u{200B}');
        assert!(is_disallowed_codepoint('\u{200B}'));
        let err = quarantine_str_field("vehicle_id", &spoofed, 256)
            .expect_err("zero-width space must be rejected");
        assert_eq!(
            err,
            QuarantineReject::FieldDisallowedCodepoint {
                field: "vehicle_id",
                codepoint: 0x200B
            }
        );
    }

    #[test]
    fn arabic_letter_mark_is_rejected() {
        // U+061C ARABIC LETTER MARK: Bidi_Control だが 0x202A..=0x202E / isolate 範囲の外に
        // 単独で存在する不可視符号点。unicode バイパス類の回帰テスト。
        let spoofed = format!("uav{}01", '\u{061C}');
        assert!(is_disallowed_codepoint('\u{061C}'));
        let err = quarantine_str_field("vehicle_id", &spoofed, 256)
            .expect_err("ARABIC LETTER MARK must be rejected");
        assert_eq!(
            err,
            QuarantineReject::FieldDisallowedCodepoint {
                field: "vehicle_id",
                codepoint: 0x061C
            }
        );
    }

    #[test]
    fn word_joiner_is_rejected() {
        // U+2060 WORD JOINER: zero-width だが 0x200B..=0x200F 範囲の外にある不可視符号点。
        // 視覚同一・バイト列相違の識別子を許すバイパスを塞ぐ（レビュー指摘2の回帰テスト）。
        let spoofed = format!("uav{}01", '\u{2060}');
        assert!(is_disallowed_codepoint('\u{2060}'));
        let err =
            quarantine_str_field("mode", &spoofed, 256).expect_err("WORD JOINER must be rejected");
        assert_eq!(
            err,
            QuarantineReject::FieldDisallowedCodepoint {
                field: "mode",
                codepoint: 0x2060
            }
        );
    }

    #[test]
    fn control_character_is_rejected() {
        let spoofed = "uav\x0101";
        let err = quarantine_str_field("vehicle_id", spoofed, 256)
            .expect_err("C0 control char must be rejected");
        assert_eq!(
            err,
            QuarantineReject::FieldDisallowedCodepoint {
                field: "vehicle_id",
                codepoint: 0x01
            }
        );
    }

    #[test]
    fn quarantine_reject_display_never_contains_semicolon() {
        // conformance `C-PROVENANCE`: provenance token に `;` を含めない
        // （下流 serializer のトップレベル区切りと衝突しない）。
        let rejects = [
            QuarantineReject::PayloadOversize {
                max_bytes: 1,
                actual_bytes: 2,
            },
            QuarantineReject::JsonNestingExceeded { max_depth: 1 },
            QuarantineReject::FrameLengthMismatch {
                expected_bytes: 1,
                actual_bytes: 2,
            },
            QuarantineReject::FieldOversize {
                field: "vehicle_id",
                max_bytes: 1,
                actual_bytes: 2,
            },
            QuarantineReject::FieldDisallowedCodepoint {
                field: "vehicle_id",
                codepoint: 0x202E,
            },
        ];
        for reject in rejects {
            assert!(!reject.to_string().contains(';'), "{reject}");
        }
    }
}

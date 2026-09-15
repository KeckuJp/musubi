use std::path::PathBuf;

use egui::{Align2, Color32, CornerRadius, FontId, Rect, Sense, Stroke, StrokeKind, pos2, vec2};
use musubi_reference_pipeline::WINDOW_SPAN_MS;
use musubi_reference_pipeline::export::{
    NOTE_CANDIDATE, NOTE_CEILING, QUESTION_7, answer_paragraph, claim_lines, outcome_lines,
    source_clock_line, stand_in_line,
};
use musubi_reference_pipeline::timefmt::{date_utc, hms_utc, pm_s, rel_s};
use musubi_reference_pipeline::{PipelineOut, SourceRun};
use musubi_reference_readers::eval::top_candidate;
use musubi_reference_types::{CauseOutcome, ChannelId, OrderRelation, order_with_bounds};

use crate::Session;
use crate::png::encode_png;

#[cfg(test)]
#[path = "guide_tests.rs"]
mod guide_tests;

const C_OBS: Color32 = Color32::from_rgb(72, 140, 92); // observed
const C_STALE: Color32 = Color32::from_rgb(214, 160, 40); // stale（値不変の継続・0 埋め）
const C_ABSENT: Color32 = Color32::from_rgb(196, 60, 50); // absent
const C_BAND: Color32 = Color32::from_rgba_premultiplied(120, 120, 200, 80); // 誤差帯
const C_CONSISTENT: Color32 = Color32::from_rgb(52, 110, 190);
const C_AMBIGUOUS: Color32 = Color32::from_rgb(220, 120, 30);
const C_UNKNOWN: Color32 = Color32::from_rgb(130, 80, 170);
const C_ORDER_UNKNOWN: Color32 = Color32::from_rgb(160, 60, 120);
const C_TEXT: Color32 = Color32::from_rgb(230, 230, 230);
const C_DIM: Color32 = Color32::from_rgb(150, 150, 150);
const C_ROWBG: Color32 = Color32::from_rgb(34, 36, 40);
const C_ROWBG2: Color32 = Color32::from_rgb(42, 44, 49);
const C_STANDIN: Color32 = Color32::from_rgb(90, 60, 20);

const LABEL_W: f32 = 300.0;
const AXIS_H: f32 = 44.0;
const ASSET_H: f32 = 26.0;
const SOURCE_H: f32 = 26.0;
const LANE_H: f32 = 14.0;
const CAUSE_H: f32 = 26.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum GuideSection {
    Overview,
    Summary,
    Timeline,
    Causes,
    Clocks,
}

impl GuideSection {
    const ALL: [Self; 5] = [
        Self::Overview,
        Self::Summary,
        Self::Timeline,
        Self::Causes,
        Self::Clocks,
    ];

    fn title(self) -> &'static str {
        match self {
            Self::Overview => "What this screen is — and is not",
            Self::Summary => "What happened",
            Self::Timeline => "What is missing, and since when",
            Self::Causes => "Which failure pattern is consistent",
            Self::Clocks => "How far to trust the clocks",
        }
    }

    fn hint(self) -> &'static str {
        match self {
            Self::Overview => "Read the input conditions and claim ceiling first.",
            Self::Summary => "One paragraph; the same answer is exported to answer.txt.",
            Self::Timeline => {
                "Read each record's gaps. Colours and patterns are explained in the legend."
            }
            Self::Causes => {
                "Click a cause box for its evidence and missing channels. A candidate, not a verdict."
            }
            Self::Clocks => {
                "Check each record's clock basis and error band before comparing times."
            }
        }
    }
}

#[derive(Debug, Clone)]
struct AbsenceBar {
    asset_idx: usize,
    source_idx: usize,
    channel: ChannelId,
    start_ms: i64,
    end_ms: i64,
    bound_ms: i64,
    clock: &'static str,
}

#[derive(Debug, Clone)]
struct OrderUnknownSpan {
    asset_idx: usize,
    from_ms: i64,
    to_ms: i64,
    what: String,
}

pub(crate) struct ScreenshotPlan {
    path: PathBuf,
    frames: u32,
    requested: bool,
    done: bool,
}

impl ScreenshotPlan {
    pub(crate) fn new(path: PathBuf) -> Self {
        Self {
            path,
            frames: 0,
            requested: false,
            done: false,
        }
    }
}

pub struct ViewerApp {
    s: Session,
    answer: String,
    stand_in: Vec<String>,
    bars: Vec<AbsenceBar>,
    order_unknown: Vec<OrderUnknownSpan>,
    view_start: f64,
    view_end: f64,
    selected: Option<usize>,
    cause_rects: Vec<(Rect, usize)>,
    screenshot: Option<ScreenshotPlan>,
    guide_visible: bool,
    guide_section: GuideSection,
    guide_requested: Option<GuideSection>,
    summary_open: bool,
}

fn outcome_color(o: &CauseOutcome) -> Color32 {
    match o {
        CauseOutcome::Consistent { .. } => C_CONSISTENT,
        CauseOutcome::Ambiguous { .. } => C_AMBIGUOUS,
        CauseOutcome::Unknown { .. } => C_UNKNOWN,
    }
}

fn outcome_word(o: &CauseOutcome) -> &'static str {
    match o {
        CauseOutcome::Consistent { .. } => "CONSISTENT WITH",
        CauseOutcome::Ambiguous { .. } => "AMBIGUOUS",
        CauseOutcome::Unknown { .. } => "UNKNOWN",
    }
}

fn nice_tick_ms(visible_ms: f64) -> i64 {
    const STEPS: [i64; 14] = [
        100, 200, 500, 1_000, 2_000, 5_000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000,
        600_000, 1_800_000,
    ];
    let target = visible_ms / 8.0;
    STEPS
        .iter()
        .copied()
        .find(|s| (*s as f64) >= target)
        .unwrap_or(3_600_000)
}

fn bucket_states(src: &SourceRun, start_ms: i64, end_ms: i64, n: usize) -> Vec<u8> {
    let mut v = vec![0u8; n.max(1)];
    if end_ms <= start_ms {
        return v;
    }
    let span = (end_ms - start_ms) as f64;
    for o in &src.obs {
        let Some(w) = o.wall_ms else {
            continue;
        };
        if w < start_ms || w >= end_ms {
            continue;
        }
        let i = (((w - start_ms) as f64 / span) * n as f64) as usize;
        let i = i.min(n - 1);
        let st = if o.stale { 1 } else { 2 };
        if st > v[i] {
            v[i] = st;
        }
    }
    v
}

fn build_bars(out: &PipelineOut) -> Vec<AbsenceBar> {
    let mut bars = Vec::new();
    for n in &out.noticed {
        let Some(ai) = out.assets.iter().position(|a| a.asset_id == n.asset_id) else {
            continue;
        };
        let Some(src) = out.assets[ai].sources.get(n.source_index) else {
            continue;
        };
        let ch = n.negative.subject.channel;
        let end = src
            .obs
            .iter()
            .filter(|o| o.channel == ch && !o.stale)
            .filter_map(|o| o.wall_ms)
            .filter(|w| *w > n.since_wall_ms)
            .min()
            .unwrap_or(out.end_ms)
            .max(n.since_wall_ms + 1);
        bars.push(AbsenceBar {
            asset_idx: ai,
            source_idx: n.source_index,
            channel: ch,
            start_ms: n.since_wall_ms,
            end_ms: end,
            bound_ms: n.bound_ms,
            clock: n.negative.clock_basis.as_label(),
        });
    }
    bars
}

fn build_order_unknown(bars: &[AbsenceBar]) -> Vec<OrderUnknownSpan> {
    let mut v: Vec<OrderUnknownSpan> = Vec::new();
    for (i, a) in bars.iter().enumerate() {
        for b in bars.iter().skip(i + 1) {
            if a.asset_idx != b.asset_idx || a.source_idx == b.source_idx {
                continue;
            }
            if (a.start_ms - b.start_ms).abs() > WINDOW_SPAN_MS {
                continue;
            }
            if a.bound_ms == 0 && b.bound_ms == 0 {
                continue; // 両方 host 軸＝順序は主張できる
            }
            let rel = order_with_bounds(
                a.start_ms * 1000,
                a.bound_ms * 1000,
                b.start_ms * 1000,
                b.bound_ms * 1000,
            );
            if rel == OrderRelation::OrderUnknown {
                let from = a.start_ms.min(b.start_ms) - a.bound_ms.max(b.bound_ms);
                let to = a.start_ms.max(b.start_ms) + a.bound_ms.max(b.bound_ms);
                let what = format!("{} vs {}", a.channel.as_str(), b.channel.as_str());
                if let Some(e) = v
                    .iter_mut()
                    .find(|e| e.asset_idx == a.asset_idx && e.from_ms <= to && from <= e.to_ms)
                {
                    e.from_ms = e.from_ms.min(from);
                    e.to_ms = e.to_ms.max(to);
                    if !e.what.contains(&what) {
                        e.what.push_str(", ");
                        e.what.push_str(&what);
                    }
                } else {
                    v.push(OrderUnknownSpan {
                        asset_idx: a.asset_idx,
                        from_ms: from,
                        to_ms: to,
                        what,
                    });
                }
            }
        }
    }
    v
}

impl ViewerApp {
    fn new(s: Session, screenshot: Option<PathBuf>) -> Self {
        let answer = answer_paragraph(&s.out);
        let stand_in: Vec<String> = s
            .out
            .assets
            .iter()
            .flat_map(|a| {
                a.sources.iter().map(move |src| {
                    format!(
                        "{} · clock: {}",
                        stand_in_line(&a.asset_id, a.family, src),
                        source_clock_line(src)
                    )
                })
            })
            .collect();
        let bars = build_bars(&s.out);
        let order_unknown = build_order_unknown(&bars);
        let (vs, ve) = (s.out.t0_ms as f64, s.out.end_ms as f64);
        let has_claims = !s.out.claims.is_empty();
        Self {
            s,
            answer,
            stand_in,
            bars,
            order_unknown,
            view_start: vs,
            view_end: ve.max(vs + 1000.0),
            selected: has_claims.then_some(0),
            cause_rects: Vec::new(),
            guide_visible: true,
            guide_section: GuideSection::Overview,
            guide_requested: None,
            summary_open: true,
            screenshot: screenshot.map(|path| ScreenshotPlan {
                path,
                frames: 0,
                requested: false,
                done: false,
            }),
        }
    }

    fn x_of(&self, plot: &Rect, t_ms: i64) -> f32 {
        let f = (t_ms as f64 - self.view_start) / (self.view_end - self.view_start);
        plot.left() + (f as f32) * plot.width()
    }

    fn t_of(&self, plot: &Rect, x: f32) -> f64 {
        let f = ((x - plot.left()) / plot.width().max(1.0)) as f64;
        self.view_start + f * (self.view_end - self.view_start)
    }

    fn reset_view(&mut self) {
        self.view_start = self.s.out.t0_ms as f64;
        self.view_end = (self.s.out.end_ms as f64).max(self.view_start + 1000.0);
    }

    fn zoom_about(&mut self, factor: f64, t_anchor: f64) {
        let span = (self.view_end - self.view_start) * factor;
        let span = span.clamp(1_000.0, 7.0 * 24.0 * 3_600_000.0);
        let f = (t_anchor - self.view_start) / (self.view_end - self.view_start);
        self.view_start = t_anchor - f * span;
        self.view_end = self.view_start + span;
    }

    fn pan_ms(&mut self, d_ms: f64) {
        self.view_start += d_ms;
        self.view_end += d_ms;
    }

    fn select_guide(&mut self, section: GuideSection) {
        self.guide_section = section;
        self.guide_requested = Some(section);
        if section == GuideSection::Summary {
            self.summary_open = true;
        }
    }

    fn toggle_guide(&mut self) {
        self.guide_visible = !self.guide_visible;
    }

    fn section_heading(&mut self, ui: &mut egui::Ui, section: GuideSection) {
        let title = format!("{}  {}", section as usize + 1, section.title());
        let selected = self.guide_section == section;
        let response = ui.label(egui::RichText::new(title).strong().size(15.0));
        if selected && self.guide_visible {
            ui.painter().line_segment(
                [response.rect.left_bottom(), response.rect.right_bottom()],
                Stroke::new(2.0, C_CONSISTENT),
            );
        }
        if self.guide_requested == Some(section) {
            response.scroll_to_me(Some(egui::Align::Min));
            self.guide_requested = None;
        }
    }

    fn guide_panel(&mut self, ui: &mut egui::Ui) {
        ui.label(egui::RichText::new("READ IN THIS ORDER").strong());
        if ui.button("Hide guide").clicked() {
            self.toggle_guide();
        }
        ui.separator();
        egui::ScrollArea::vertical()
            .id_salt("guide_scroll")
            .show(ui, |ui| {
                for section in GuideSection::ALL {
                    let title = format!("{}  {}", section as usize + 1, section.title());
                    if ui
                        .add(
                            egui::Button::selectable(
                                self.guide_section == section,
                                egui::RichText::new(title).strong(),
                            )
                            .wrap(),
                        )
                        .clicked()
                    {
                        self.select_guide(section);
                    }
                    ui.label(egui::RichText::new(section.hint()).size(12.0).color(C_DIM));
                    ui.add_space(12.0);
                }
                ui.separator();
                ui.label("What to do next is decided by a person, not by this screen.");
                ui.add_space(8.0);
                ui.label(
                    egui::RichText::new("Exports: answer.txt, absences.csv, cause_claims.csv")
                        .small(),
                );
            });
    }

    fn top_panel(&mut self, ui: &mut egui::Ui) {
        ui.horizontal_wrapped(|ui| {
            ui.heading("Musubi pre-demo viewer");
            ui.label(
                egui::RichText::new("reference consumer / demo viewer — not a product surface · offline · no network · read-only · deterministic (no AI)")
                    .color(C_DIM),
            );
        });
        self.section_heading(ui, GuideSection::Overview);
        ui.horizontal_wrapped(|ui| {
            ui.label(format!(
                "folder: {}  ·  profiles: {}  ·  {} files / {} assets / {} absences / {} cause windows",
                self.s.input_dir.display(),
                self.s.profiles_root.display(),
                self.s.out.files.len(),
                self.s.out.assets.len(),
                self.s.out.noticed.len(),
                self.s.out.claims.len()
            ));
        });
        ui.label(format!(
            "Input status: {} record files loaded · {} entries not read",
            self.s.loaded.files.len(),
            self.s.loaded.skipped.len()
        ));
        if !self.s.loaded.skipped.is_empty() {
            egui::CollapsingHeader::new(format!(
                "Entries not read ({})",
                self.s.loaded.skipped.len()
            ))
            .show(ui, |ui| {
                for (name, reason) in &self.s.loaded.skipped {
                    ui.label(format!("{name} — {reason}"));
                }
                ui.label("These entries were not read as records. This is not partial acceptance of a corrupt recording.");
            });
        }
        let n_public = self
            .s
            .out
            .assets
            .iter()
            .flat_map(|a| a.sources.iter())
            .filter(|s| s.profile.origin == "public")
            .count();
        let n_all: usize = self.s.out.assets.iter().map(|a| a.sources.len()).sum();
        egui::Frame::new()
            .fill(C_STANDIN)
            .inner_margin(egui::Margin::same(6))
            .corner_radius(CornerRadius::same(3))
            .show(ui, |ui| {
                ui.label(
                    egui::RichText::new(format!(
                        "STAND-IN: {n_public} of {n_all} records are read with the bundled example profiles (ArduPilot / PX4 / Betaflight / EdgeTX / DVR presence) — our assumptions, not the partner's. {}",
                        if n_public == n_all { "No partner profile is loaded." } else { "Records marked [partner] use a partner-supplied profile." }
                    ))
                    .color(C_TEXT)
                    .strong(),
                );
                ui.label(
                    egui::RichText::new(format!("CLAIM CEILING: {NOTE_CEILING}"))
                        .color(C_TEXT)
                        .italics(),
                );
            });
        ui.horizontal(|ui| {
            self.section_heading(ui, GuideSection::Summary);
            let label = if self.summary_open {
                "Hide summary"
            } else {
                "Show summary"
            };
            if ui.small_button(label).clicked() {
                self.summary_open = !self.summary_open;
            }
        });
        if self.summary_open {
            ui.label(egui::RichText::new("Question 7 · answer (same text as answer.txt)").strong());
            ui.label(egui::RichText::new(QUESTION_7).strong());
            ui.label(self.answer.as_str());
            ui.label(
                    egui::RichText::new("Per window: see the cause lane (click a box) or answer.txt. Every cause is a candidate, not a verdict.")
                        .color(C_DIM),
                );
        }
    }

    fn right_panel(&mut self, ui: &mut egui::Ui) {
        self.section_heading(ui, GuideSection::Causes);
        ui.label(
            egui::RichText::new(format!(
                "Match against a known failure pattern (signature here is not a cryptographic signature) = {NOTE_CANDIDATE}. No verdict, recommendation or command is produced."
            ))
            .color(C_DIM),
        );
        ui.separator();
        egui::ScrollArea::vertical()
            .id_salt("claim_scroll")
            .auto_shrink([false, false])
            .show(ui, |ui| {
                if let Some(i) = self.selected {
                    let o = &self.s.out.claims[i].claim.outcome;
                    let (state, main, alt, missing) = outcome_lines(&self.s.knowledge, o);
                    ui.colored_label(
                        outcome_color(o),
                        egui::RichText::new(format!("{} — {}", outcome_word(o), state)).strong(),
                    );
                    ui.label(egui::RichText::new("consistent with (candidate)").color(C_DIM));
                    ui.label(main);
                    ui.label(egui::RichText::new("alternatives").color(C_DIM));
                    ui.label(alt);
                    ui.label(
                        egui::RichText::new("what would disambiguate / missing channel")
                            .color(C_DIM),
                    );
                    ui.label(missing);
                    if let CauseOutcome::Consistent { candidates }
                        | CauseOutcome::Ambiguous { candidates, .. } = o {
                        ui.separator();
                        ui.label(egui::RichText::new("Cues matched").strong());
                        for candidate in candidates {
                            ui.label(format!("{}: {}", candidate.signature_id,
                                candidate.signature_match.matched_cues.join(", ")));
                        }
                    }
                    ui.separator();
                    egui::CollapsingHeader::new("Provenance (as exported)").show(ui, |ui| {
                        ui.label("Degraded is an annotation in this build, not a judgement about the vehicle or data. Input confidence is not a probability; a file-derived 0.00 means not provided.");
                        for line in claim_lines(&self.s.out, &self.s.knowledge, i) {
                            ui.label(egui::RichText::new(line).monospace().size(11.0));
                        }
                    });
                } else {
                    ui.label("Click a cause box on the timeline (or pick one below).");
                }
                ui.separator();
                ui.label(egui::RichText::new("All windows (record order, not action priority)").strong());
                let claims: Vec<(usize, String, Color32)> = self
                    .s
                    .out
                    .claims
                    .iter()
                    .enumerate()
                    .map(|(i, c)| {
                        let (state, main, _, _) =
                            outcome_lines(&self.s.knowledge, &c.claim.outcome);
                        (
                            i,
                            format!(
                                "{} {} {} · {}: {}",
                                c.asset_id,
                                hms_utc(c.window_start_ms),
                                rel_s(c.window_start_ms - self.s.out.t0_ms),
                                state,
                                main.chars().take(60).collect::<String>()
                            ),
                            outcome_color(&c.claim.outcome),
                        )
                    })
                    .collect();
                for (i, text, color) in claims {
                    let sel = self.selected == Some(i);
                    if ui
                        .selectable_label(sel, egui::RichText::new(text).color(color).size(11.0))
                        .clicked()
                    {
                        self.selected = Some(i);
                    }
                }
                if let Some(m) = &self.s.metrics {
                    ui.separator();
                    ui.label(
                        egui::RichText::new(
                            "Reference metrics vs synthetic ground truth (numbers only; no threshold)",
                        )
                        .strong(),
                    );
                    ui.label(egui::RichText::new(m.summary()).monospace().size(10.0));
                }
            });
    }

    fn bottom_panel(&mut self, ui: &mut egui::Ui) {
        self.section_heading(ui, GuideSection::Clocks);
        ui.label(
            egui::RichText::new("Clock assumptions per record (recorded clock model) — shared axis = host receive time + estimated offset; absence is judged on each record's own axis; cross-record order is claimed only when bands do not overlap (else ORDER_UNKNOWN).")
                .color(C_DIM)
                .size(11.0),
        );
        egui::ScrollArea::vertical()
            .id_salt("clock_scroll")
            .max_height(if self.guide_section == GuideSection::Clocks {
                170.0
            } else {
                76.0
            })
            .show(ui, |ui| {
                for line in &self.stand_in {
                    ui.label(egui::RichText::new(line).size(12.0));
                }
            });
        ui.label(
            egui::RichText::new("wheel = zoom at pointer · drag = pan · R = reset · click a cause box = details · states are written as text as well as colour (OBS / STALE / ABSENT)")
                .color(C_DIM)
                .size(10.0),
        );
    }

    fn timeline(&mut self, ui: &mut egui::Ui) {
        self.section_heading(ui, GuideSection::Timeline);
        ui.horizontal_wrapped(|ui| {
            for (color, label) in [
                (C_OBS, "OBS · received"),
                (C_STALE, "STALE · value frozen"),
                (C_ABSENT, "ABSENT · none received"),
            ] {
                let (r, _) = ui.allocate_exact_size(vec2(10.0, 10.0), Sense::hover());
                ui.painter().rect_filled(r, CornerRadius::ZERO, color);
                ui.label(egui::RichText::new(label).size(12.0));
            }
            ui.label(
                egui::RichText::new(
                    "± error band · hatched: ORDER_UNKNOWN (order cannot be established)",
                )
                .size(12.0),
            );
        });
        let mut total_h = AXIS_H + 8.0;
        for (ai, a) in self.s.out.assets.iter().enumerate() {
            total_h += ASSET_H + CAUSE_H;
            for (si, _) in a.sources.iter().enumerate() {
                total_h += SOURCE_H + LANE_H * self.lanes_for(ai, si).len() as f32;
            }
        }
        let avail = ui.available_size();
        let size = vec2(avail.x, avail.y.max(total_h));
        egui::ScrollArea::vertical()
            .id_salt("timeline_scroll")
            .auto_shrink([false, false])
            .show(ui, |ui| {
                let (resp, painter) = ui.allocate_painter(size, Sense::click_and_drag());
                let full = resp.rect;
                let label_width = (full.width() * 0.5).clamp(150.0, LABEL_W);
                let plot = Rect::from_min_max(
                    pos2(full.left() + label_width, full.top()),
                    pos2(full.right() - 8.0, full.bottom()),
                );
                let hover = resp.hover_pos();
                let (scroll, zoom, key_r) = ui.input(|i| {
                    (
                        i.smooth_scroll_delta,
                        i.zoom_delta(),
                        i.key_pressed(egui::Key::R),
                    )
                });
                if key_r {
                    self.reset_view();
                }
                if let Some(p) = hover
                    && plot.contains(p)
                {
                    let t = self.t_of(&plot, p.x);
                    if scroll.y.abs() > 0.0 {
                        let factor = (1.0 - f64::from(scroll.y) * 0.002).clamp(0.5, 2.0);
                        self.zoom_about(factor, t);
                    }
                    if (zoom - 1.0).abs() > 1e-3 {
                        self.zoom_about(1.0 / f64::from(zoom), t);
                    }
                    if scroll.x.abs() > 0.0 {
                        let d = -f64::from(scroll.x) / f64::from(plot.width())
                            * (self.view_end - self.view_start);
                        self.pan_ms(d);
                    }
                }
                if resp.dragged() {
                    let dx = resp.drag_delta().x;
                    let d = -f64::from(dx) / f64::from(plot.width())
                        * (self.view_end - self.view_start);
                    self.pan_ms(d);
                }
                if resp.clicked()
                    && let Some(p) = resp.interact_pointer_pos()
                {
                    if let Some((_, i)) = self.cause_rects.iter().find(|(r, _)| r.contains(p)) {
                        self.selected = Some(*i);
                    }
                }
                self.paint(&painter, full, plot);
            });
    }

    fn lanes_for(&self, ai: usize, si: usize) -> Vec<ChannelId> {
        let mut v: Vec<ChannelId> = self
            .bars
            .iter()
            .filter(|b| b.asset_idx == ai && b.source_idx == si)
            .map(|b| b.channel)
            .collect();
        v.sort();
        v.dedup();
        v
    }

    fn paint(&mut self, painter: &egui::Painter, full: Rect, plot: Rect) {
        let font = FontId::monospace(11.0);
        let font_s = FontId::monospace(10.0);
        let font_b = FontId::proportional(12.0);
        painter.rect_filled(full, CornerRadius::ZERO, Color32::from_rgb(28, 29, 33));
        let axis = Rect::from_min_size(pos2(full.left(), full.top()), vec2(full.width(), AXIS_H));
        painter.rect_filled(axis, CornerRadius::ZERO, C_ROWBG2);
        painter.text(
            pos2(full.left() + 6.0, axis.top() + 10.0),
            Align2::LEFT_CENTER,
            format!(
                "shared axis · UTC {} · host receive time + est. offset",
                date_utc(self.view_start as i64)
            ),
            font_s.clone(),
            C_TEXT,
        );
        painter.text(
            pos2(full.left() + 6.0, axis.top() + 26.0),
            Align2::LEFT_CENTER,
            format!(
                "view {} → {} ({}s) · ± = error band · hatched = ORDER_UNKNOWN",
                hms_utc(self.view_start as i64),
                hms_utc(self.view_end as i64),
                ((self.view_end - self.view_start) / 1000.0).round() as i64
            ),
            font_s.clone(),
            C_DIM,
        );
        let tick = nice_tick_ms(self.view_end - self.view_start);
        let first = (self.view_start as i64).div_euclid(tick) * tick;
        let mut t = first;
        let clip = painter.with_clip_rect(Rect::from_min_max(
            pos2(plot.left(), full.top()),
            pos2(plot.right(), full.bottom()),
        ));
        while (t as f64) <= self.view_end {
            let x = self.x_of(&plot, t);
            clip.line_segment(
                [pos2(x, axis.top() + 30.0), pos2(x, full.bottom())],
                Stroke::new(1.0, Color32::from_rgb(55, 57, 62)),
            );
            clip.text(
                pos2(x + 2.0, axis.top() + 36.0),
                Align2::LEFT_CENTER,
                format!("{} {}", hms_utc(t), rel_s(t - self.s.out.t0_ms)),
                font_s.clone(),
                C_TEXT,
            );
            t += tick;
        }
        let x0 = self.x_of(&plot, self.s.out.t0_ms);
        let x1 = self.x_of(&plot, self.s.out.end_ms);
        if x0 > plot.left() {
            clip.rect_filled(
                Rect::from_min_max(pos2(plot.left(), axis.bottom()), pos2(x0, full.bottom())),
                CornerRadius::ZERO,
                Color32::from_rgba_premultiplied(0, 0, 0, 90),
            );
        }
        if x1 < plot.right() {
            clip.rect_filled(
                Rect::from_min_max(pos2(x1, axis.bottom()), pos2(plot.right(), full.bottom())),
                CornerRadius::ZERO,
                Color32::from_rgba_premultiplied(0, 0, 0, 90),
            );
        }
        self.cause_rects.clear();
        let mut y = axis.bottom() + 4.0;
        let n_px = plot.width().max(1.0) as usize;
        let (vs, ve) = (self.view_start as i64, self.view_end as i64);
        for ai in 0..self.s.out.assets.len() {
            let a = &self.s.out.assets[ai];
            let hr = Rect::from_min_size(pos2(full.left(), y), vec2(full.width(), ASSET_H));
            painter.rect_filled(hr, CornerRadius::ZERO, Color32::from_rgb(50, 52, 58));
            let origins: Vec<&str> = a
                .sources
                .iter()
                .map(|s| s.profile.origin.as_str())
                .collect();
            let all_public = origins.iter().all(|o| *o == "public");
            painter.text(
                pos2(full.left() + 6.0, hr.center().y),
                Align2::LEFT_CENTER,
                format!(
                    "{}  family={}  [{}]",
                    a.asset_id,
                    a.family.as_str(),
                    if all_public {
                        "stand-in: public default profiles"
                    } else {
                        "partner profile present"
                    }
                ),
                font_b.clone(),
                C_TEXT,
            );
            for ou in self.order_unknown.iter().filter(|o| o.asset_idx == ai) {
                let xa = self.x_of(&plot, ou.from_ms);
                let xb = self.x_of(&plot, ou.to_ms);
                let r = Rect::from_min_max(pos2(xa, hr.top() + 4.0), pos2(xb, hr.bottom() - 4.0));
                hatch(&clip, r, C_ORDER_UNKNOWN);
                clip.rect_stroke(
                    r,
                    CornerRadius::ZERO,
                    Stroke::new(1.0, C_ORDER_UNKNOWN),
                    StrokeKind::Inside,
                );
                clip.text(
                    pos2(xb + 4.0, hr.center().y),
                    Align2::LEFT_CENTER,
                    format!("ORDER_UNKNOWN ({}): bands overlap", ou.what),
                    font_s.clone(),
                    C_ORDER_UNKNOWN,
                );
            }
            y += ASSET_H;
            for (si, src) in a.sources.iter().enumerate() {
                let lanes = self.lanes_for(ai, si);
                let row_h = SOURCE_H + LANE_H * lanes.len() as f32;
                let rr = Rect::from_min_size(pos2(full.left(), y), vec2(full.width(), row_h));
                painter.rect_filled(
                    rr,
                    CornerRadius::ZERO,
                    if si % 2 == 0 { C_ROWBG } else { C_ROWBG2 },
                );
                let bound = src.alignment.offset.map_or(0, |o| o.bound_us / 1000);
                painter.text(
                    pos2(full.left() + 6.0, y + 9.0),
                    Align2::LEFT_CENTER,
                    format!(
                        "{} · {} [{}]",
                        src.profile.source_role.as_str(),
                        src.profile.profile_id,
                        src.profile.origin
                    ),
                    font.clone(),
                    C_TEXT,
                );
                painter.text(
                    pos2(full.left() + 6.0, y + 20.0),
                    Align2::LEFT_CENTER,
                    format!(
                        "clock: {} {}",
                        src.alignment.basis.as_label(),
                        if src.host_axis() {
                            String::new()
                        } else {
                            pm_s(bound)
                        }
                    ),
                    font_s.clone(),
                    C_DIM,
                );
                let band = Rect::from_min_max(
                    pos2(plot.left(), y + 4.0),
                    pos2(plot.right(), y + SOURCE_H - 4.0),
                );
                let states = bucket_states(src, vs, ve, n_px);
                let px = band.width() / states.len() as f32;
                let mut i = 0;
                while i < states.len() {
                    let st = states[i];
                    let mut j = i + 1;
                    while j < states.len() && states[j] == st {
                        j += 1;
                    }
                    if st > 0 {
                        let r = Rect::from_min_max(
                            pos2(band.left() + i as f32 * px, band.top()),
                            pos2(band.left() + j as f32 * px, band.bottom()),
                        );
                        clip.rect_filled(
                            r,
                            CornerRadius::ZERO,
                            if st == 2 { C_OBS } else { C_STALE },
                        );
                    }
                    i = j;
                }
                clip.text(
                    pos2(plot.left() + 3.0, band.center().y),
                    Align2::LEFT_CENTER,
                    format!(
                        "{} obs{}",
                        src.obs.len(),
                        if src.obs.iter().any(|o| o.stale) {
                            " (STALE marked amber)"
                        } else {
                            ""
                        }
                    ),
                    font_s.clone(),
                    Color32::from_rgb(240, 240, 240),
                );
                for (li, ch) in lanes.iter().enumerate() {
                    let ly = y + SOURCE_H + li as f32 * LANE_H;
                    painter.text(
                        pos2(full.left() + 14.0, ly + LANE_H / 2.0),
                        Align2::LEFT_CENTER,
                        format!("absent: {}", ch.as_str()),
                        font_s.clone(),
                        C_ABSENT,
                    );
                    for b in self
                        .bars
                        .iter()
                        .filter(|b| b.asset_idx == ai && b.source_idx == si && b.channel == *ch)
                    {
                        let xa = self.x_of(&plot, b.start_ms);
                        let xb = self.x_of(&plot, b.end_ms).max(xa + 2.0);
                        if b.bound_ms > 0 {
                            let ba = self.x_of(&plot, b.start_ms - b.bound_ms);
                            let bb = self.x_of(&plot, b.start_ms + b.bound_ms);
                            clip.rect_filled(
                                Rect::from_min_max(pos2(ba, ly + 1.0), pos2(bb, ly + LANE_H - 1.0)),
                                CornerRadius::ZERO,
                                C_BAND,
                            );
                        }
                        let r = Rect::from_min_max(pos2(xa, ly + 2.0), pos2(xb, ly + LANE_H - 2.0));
                        clip.rect_filled(r, CornerRadius::ZERO, C_ABSENT);
                        clip.text(
                            pos2(xa + 3.0, ly + LANE_H / 2.0),
                            Align2::LEFT_CENTER,
                            format!(
                                "ABSENT {} since {} {} {}",
                                b.channel.as_str(),
                                hms_utc(b.start_ms),
                                b.clock,
                                if b.bound_ms > 0 {
                                    pm_s(b.bound_ms)
                                } else {
                                    String::new()
                                }
                            ),
                            font_s.clone(),
                            Color32::WHITE,
                        );
                    }
                }
                y += row_h;
            }
            let cr = Rect::from_min_size(pos2(full.left(), y), vec2(full.width(), CAUSE_H));
            painter.rect_filled(cr, CornerRadius::ZERO, Color32::from_rgb(38, 40, 46));
            painter.text(
                pos2(full.left() + 6.0, cr.center().y),
                Align2::LEFT_CENTER,
                "cause candidates · cons=consistent with / ambig=ambiguous / unk=unknown · not a verdict",
                font_s.clone(),
                C_DIM,
            );
            for (ci, c) in self.s.out.claims.iter().enumerate() {
                if c.asset_id != a.asset_id {
                    continue;
                }
                let xa = self.x_of(&plot, c.window_start_ms);
                let xb = self.x_of(&plot, c.window_end_ms).max(xa + 4.0);
                let r = Rect::from_min_max(pos2(xa, cr.top() + 3.0), pos2(xb, cr.bottom() - 3.0));
                let col = outcome_color(&c.claim.outcome);
                clip.rect_filled(r, CornerRadius::same(2), col);
                if self.selected == Some(ci) {
                    clip.rect_stroke(
                        r.expand(2.0),
                        CornerRadius::same(2),
                        Stroke::new(2.0, Color32::WHITE),
                        StrokeKind::Outside,
                    );
                }
                let label = match &c.claim.outcome {
                    CauseOutcome::Consistent { .. } => format!(
                        "cons: {}",
                        top_candidate(&c.claim.outcome).map_or(String::new(), |(id, _, _)| id)
                    ),
                    CauseOutcome::Ambiguous { candidates, .. } => format!(
                        "ambig: {}",
                        candidates
                            .iter()
                            .map(|x| x.signature_id.as_str())
                            .collect::<Vec<_>>()
                            .join(" / ")
                    ),
                    CauseOutcome::Unknown { reason } => {
                        format!("unk: {}", reason.as_label())
                    }
                };
                let boxed = clip.with_clip_rect(r.intersect(plot));
                boxed.text(
                    pos2(xa + 4.0, cr.center().y),
                    Align2::LEFT_CENTER,
                    label,
                    font_s.clone(),
                    Color32::WHITE,
                );
                self.cause_rects.push((r, ci));
            }
            y += CAUSE_H;
        }
    }

    fn handle_screenshot(&mut self, ctx: &egui::Context) {
        capture_screenshot(&mut self.screenshot, ctx);
    }
}

pub(crate) fn capture_screenshot(screenshot: &mut Option<ScreenshotPlan>, ctx: &egui::Context) {
    let Some(plan) = screenshot.as_mut() else {
        return;
    };
    if plan.done {
        return;
    }
    plan.frames += 1;
    ctx.request_repaint();
    if plan.frames >= 3 && !plan.requested {
        plan.requested = true;
        ctx.send_viewport_cmd(egui::ViewportCommand::Screenshot(egui::UserData::default()));
    }
    let image = ctx.input(|i| {
        i.events.iter().find_map(|e| match e {
            egui::Event::Screenshot { image, .. } => Some(image.clone()),
            _ => None,
        })
    });
    if let Some(img) = image {
        let [w, h] = img.size;
        let mut rgba = Vec::with_capacity(w * h * 4);
        for p in &img.pixels {
            rgba.extend_from_slice(&[p.r(), p.g(), p.b(), 255]);
        }
        let (w, h, rgba) = if ctx.pixels_per_point() >= 1.5 && w >= 2 && h >= 2 {
            downsample_2x(w, h, &rgba)
        } else {
            (w, h, rgba)
        };
        let bytes = encode_png(w as u32, h as u32, &rgba);
        match std::fs::write(&plan.path, bytes) {
            Ok(()) => eprintln!("screenshot written: {} ({w}x{h})", plan.path.display()),
            Err(e) => eprintln!("screenshot failed: {e}"),
        }
        plan.done = true;
        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
    }
}

fn downsample_2x(w: usize, h: usize, rgba: &[u8]) -> (usize, usize, Vec<u8>) {
    let (w2, h2) = (w / 2, h / 2);
    let mut out = Vec::with_capacity(w2 * h2 * 4);
    for y in 0..h2 {
        for x in 0..w2 {
            for c in 0..4 {
                let mut acc = 0u32;
                for (dy, dx) in [(0, 0), (0, 1), (1, 0), (1, 1)] {
                    acc += u32::from(rgba[((2 * y + dy) * w + (2 * x + dx)) * 4 + c]);
                }
                out.push((acc / 4) as u8);
            }
        }
    }
    (w2, h2, out)
}

fn hatch(p: &egui::Painter, r: Rect, color: Color32) {
    let step = 6.0;
    let mut x = r.left() - r.height();
    while x < r.right() {
        p.line_segment(
            [pos2(x, r.bottom()), pos2(x + r.height(), r.top())],
            Stroke::new(1.0, color),
        );
        x += step;
    }
}

impl ViewerApp {
    fn draw(&mut self, ui: &mut egui::Ui) {
        if self.guide_visible {
            egui::Panel::left("guide")
                .resizable(false)
                .exact_size(190.0)
                .show(ui, |ui| self.guide_panel(ui));
        }
        let top_height = (ui.available_height() * 0.34).clamp(200.0, 300.0);
        egui::Panel::top("top")
            .exact_size(top_height)
            .resizable(false)
            .show(ui, |ui| {
                if !self.guide_visible && ui.button("Show guide").clicked() {
                    self.toggle_guide();
                }
                egui::ScrollArea::vertical()
                    .id_salt("overview_scroll")
                    .max_height(300.0)
                    .show(ui, |ui| self.top_panel(ui));
            });
        egui::Panel::bottom("bottom")
            .resizable(true)
            .show(ui, |ui| self.bottom_panel(ui));
        let right_width = (ui.available_width() * 0.35).clamp(240.0, 400.0);
        egui::Panel::right("claim")
            .resizable(true)
            .default_size(400.0)
            .max_size(right_width)
            .min_size(220.0)
            .show(ui, |ui| self.right_panel(ui));
        egui::CentralPanel::default().show(ui, |ui| self.timeline(ui));
    }
}

impl eframe::App for ViewerApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        self.handle_screenshot(&ctx);
        self.draw(ui);
    }
}

pub fn run(session: Session, screenshot: Option<PathBuf>) -> eframe::Result {
    let app = ViewerApp::new(session, screenshot);
    eframe::run_native(
        "musubi-reference-viewer",
        native_options(),
        Box::new(move |_cc| Ok(Box::new(app))),
    )
}

pub(crate) fn native_options() -> eframe::NativeOptions {
    eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("Musubi pre-demo viewer (reference consumer · offline)")
            .with_inner_size([1480.0, 940.0])
            .with_min_inner_size([900.0, 600.0]),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn downsample_averages_2x2_blocks() {
        let px = [
            0u8, 0, 0, 255, 255, 255, 255, 255, 255, 255, 255, 255, 0, 0, 0, 255,
        ];
        let (w, h, out) = downsample_2x(2, 2, &px);
        assert_eq!((w, h), (1, 1));
        assert_eq!(out, vec![127, 127, 127, 255]);
    }

    #[test]
    fn bucket_states_prefers_observed_over_stale_in_a_bucket() {
        let k = musubi_reference_pipeline::Knowledge::repo_public();
        let tl =
            musubi_reference_scenario::generate(&musubi_reference_scenario::pre_demo_default(42));
        let out = musubi_reference_pipeline::run_pipeline_with(&tl, &k);
        let src = &out.assets[0].sources[0];
        let v = bucket_states(src, out.t0_ms, out.end_ms, 180);
        assert_eq!(v.len(), 180);
        assert!(v.iter().any(|s| *s == 2));
    }

    #[test]
    fn tick_steps_are_nice_and_scale_with_span() {
        assert_eq!(nice_tick_ms(8_000.0), 1_000);
        assert_eq!(nice_tick_ms(180_000.0), 30_000);
        assert_eq!(nice_tick_ms(3_600_000.0 * 24.0), 3_600_000);
    }

    #[test]
    fn order_unknown_only_when_bands_overlap_across_sources() {
        let bar = |src: usize, start: i64, bound: i64, ch: ChannelId| AbsenceBar {
            asset_idx: 0,
            source_idx: src,
            channel: ch,
            start_ms: start,
            end_ms: start + 5_000,
            bound_ms: bound,
            clock: "x",
        };
        assert!(
            build_order_unknown(&[
                bar(0, 1000, 0, ChannelId::Heartbeat),
                bar(1, 2000, 0, ChannelId::Rc)
            ])
            .is_empty()
        );
        let v = build_order_unknown(&[
            bar(0, 1000, 0, ChannelId::Heartbeat),
            bar(1, 2000, 10_000, ChannelId::Onboard),
        ]);
        assert_eq!(v.len(), 1);
        assert!(v[0].what.contains("heartbeat") && v[0].what.contains("onboard"));
        assert!(
            build_order_unknown(&[
                bar(0, 1000, 0, ChannelId::Heartbeat),
                bar(0, 2000, 10_000, ChannelId::Rc)
            ])
            .is_empty()
        );
        assert!(
            build_order_unknown(&[
                bar(0, 1000, 0, ChannelId::Heartbeat),
                bar(1, 20_000, 1_000, ChannelId::Rc)
            ])
            .is_empty()
        );
    }
}

use crate::app::{ScreenshotPlan, capture_screenshot, native_options};
use crate::detached::{DetachedRecording, time_label, value_label};
use egui::{Color32, RichText};
use std::path::PathBuf;

struct DetachedApp {
    recording: DetachedRecording,
    selected: usize,
    screenshot: Option<ScreenshotPlan>,
}

impl eframe::App for DetachedApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        capture_screenshot(&mut self.screenshot, &ui.ctx().clone());
        egui::Panel::top("top").show(ui, |ui| {
            ui.heading("Musubi pre-demo viewer");
            ui.label("reference consumer / demo viewer | offline | read-only | no network | no control");
            ui.colored_label(Color32::YELLOW, "RECORDED OBSERVATIONS | Opened after capture | Live updates: OFF");
            ui.label("Detached candidate semantics. No canonical COM, geographic position, calibration or physical-origin guarantee.");
        });
        egui::Panel::bottom("bottom").show(ui, |ui| {
            ui.label(format!("Input: {}", self.recording.input.display()));
            ui.label(format!("Exact recording digest: {}", self.recording.digest));
            ui.label("Claim ceiling: technical recorded-file inspection only; no product / field / partner / UX acceptance.");
        });
        let observation = &self.recording.records[self.selected];
        egui::Panel::right("claim").default_size(460.0).resizable(true).show(ui, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                ui.heading("Selected observation");
                ui.label(RichText::new(value_label(observation)).size(32.0).strong());
                ui.label(time_label(observation));
                ui.label("Receive UTC: UNKNOWN in this NDJSON; never inferred from boot time.");
                ui.separator();
                ui.label(format!("Semantic: {} / {} / {}", observation.namespace, observation.semantic_id, observation.semantic_version));
                ui.label("Registry admission: not asserted (unknown keys preserved)");
                ui.label(format!("Source: {}", observation.source.source_id));
                ui.label(format!("Sensor: {}", observation.source.sensor_location));
                ui.label(format!("Observation kind: {:?}", observation.observation_kind));
                ui.label("A source label is not authenticated proof of physical origin.");
                ui.separator();
                ui.label(format!("Quality: {:?}", observation.quality.status));
                ui.label(format!("Flags: {:?}", observation.quality.flags));
                if observation.missing.is_none() { ui.label("No explicit missing value in this record"); }
                if let Some(range) = &observation.valid_range {
                    ui.label(format!("Declared range: {} to {} {}", range.minimum, range.maximum, observation.unit.as_deref().unwrap_or("")));
                }
                if let Some(resolution) = observation.resolution {
                    ui.label(format!("Resolution: {resolution} {} (not sensor accuracy)", observation.unit.as_deref().unwrap_or("")));
                }
                ui.separator();
                ui.label("Cause: UNKNOWN (no cause analysis for detached records)");
                ui.label("CONSISTENT WITH means a candidate, never a confirmed cause.");
                ui.label("ABSENT means an explicit missing value. A file gap alone does not prove sensor absence.");
                ui.label("ORDER_UNKNOWN across sources or reboots: no shared clock alignment is inferred.");
                ui.separator();
                ui.label("Raw artifact references (content not authenticated by this viewer):");
                for artifact in &observation.raw_artifacts {
                    ui.label(artifact.digest.as_str());
                }
                if let Some(transformer) = &observation.transformer {
                    ui.label(format!("Transformer: {} / {}", transformer.transformer_id, transformer.transformer_version));
                    ui.label(transformer.configuration_digest.as_str());
                }
            });
        });
        egui::CentralPanel::default().show(ui, |ui| {
            ui.heading("TIMELINE / recorded file order");
            ui.label(format!("{} observations | Select a row for provenance and uncertainty", self.recording.records.len()));
            ui.label("Native event times are preserved. Rows are not a globally ordered or UTC-aligned timeline.");
            ui.separator();
            egui::ScrollArea::vertical().show_rows(ui, 56.0, self.recording.records.len(), |ui, rows| {
                for index in rows {
                    let row = &self.recording.records[index];
                    let label = format!("#{}   {}   |   {}\n{}   |   {:?}", index + 1,
                        time_label(row), value_label(row), row.source.source_id, row.quality.status);
                    if ui.selectable_label(self.selected == index, label).clicked() {
                        self.selected = index;
                    }
                }
            });
        });
    }
}

pub fn run(recording: DetachedRecording, screenshot: Option<PathBuf>) -> eframe::Result {
    let app = DetachedApp {
        recording,
        selected: 0,
        screenshot: screenshot.map(ScreenshotPlan::new),
    };
    eframe::run_native(
        "musubi-reference-viewer",
        native_options(),
        Box::new(move |cc| {
            cc.egui_ctx.set_theme(egui::ThemePreference::Dark);
            cc.egui_ctx.set_visuals(egui::Visuals::dark());
            let mut style = (*cc.egui_ctx.style_of(egui::Theme::Dark)).clone();
            style
                .text_styles
                .insert(egui::TextStyle::Body, egui::FontId::proportional(16.0));
            style
                .text_styles
                .insert(egui::TextStyle::Button, egui::FontId::proportional(16.0));
            cc.egui_ctx.set_style_of(egui::Theme::Dark, style);
            Ok(Box::new(app))
        }),
    )
}

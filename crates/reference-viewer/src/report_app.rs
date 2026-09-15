use crate::app::{ScreenshotPlan, capture_screenshot, native_options};
use crate::report::Report;
use egui::{Color32, RichText};
use serde_json::Value;
use std::path::PathBuf;

pub(crate) struct ReportApp {
    pub(crate) report: Report,
    pub(crate) selected: usize,
    screenshot: Option<ScreenshotPlan>,
}
fn label(v: &Value) -> String {
    if v.is_null() {
        return "UNKNOWN / null".into();
    }
    let text = v.as_str().map_or_else(|| v.to_string(), str::to_owned);
    if text.chars().count() > 512 {
        format!(
            "{} … [display shortened; full value retained in the report]",
            text.chars().take(512).collect::<String>()
        )
    } else {
        text
    }
}
impl ReportApp {
    pub(crate) fn new(report: Report, screenshot: Option<PathBuf>) -> Self {
        Self {
            report,
            selected: 0,
            screenshot: screenshot.map(ScreenshotPlan::new),
        }
    }
    pub(crate) fn draw(&mut self, ui: &mut egui::Ui) {
        capture_screenshot(&mut self.screenshot, &ui.ctx().clone());
        let document = &self.report.document;
        let rows = document["observations"]
            .as_array()
            .map_or(&[][..], Vec::as_slice);
        egui::Panel::top("report-header").show(ui,|ui| {
            ui.heading("Musubi recorded observation viewer");
            ui.label("Saved report | Offline | Read-only | Live updates: OFF");
            ui.colored_label(Color32::YELLOW,"UNSEALED OBSERVATIONS — declared meanings, units and clocks; no device authentication");
            ui.label("Select a row to inspect its values and provenance. File order is not a global time order.");
        });
        egui::Panel::bottom("report-provenance").resizable(true).default_size(160.0).show(ui,|ui| {
            egui::ScrollArea::vertical().show(ui,|ui| {
                ui.label(format!("Report file SHA-256 (computed): {}",self.report.file_sha256));
                ui.label(format!("Embedded source SHA-256 (bytes checked): {}",label(&document["input_sha256"])));
                ui.label("A matching digest establishes byte consistency, not authorship or physical origin.");
                ui.label(format!("Profile SHA-256 (reported, bytes unavailable): {}",label(&document["profile_sha256"])));
                ui.label(format!("Meanings SHA-256 (reported, bytes unavailable): {}",label(&document["meanings_sha256"])));
                ui.label(format!("Source records: {} | observations: {} | source bytes: {}",label(&document["source_record_count"]),rows.len(),label(&document["source_bytes"])));
                ui.label(format!("Format accounting: {}",label(&document["accounting"])));
            });
        });
        egui::Panel::right("report-details").resizable(true).default_size(650.0).show(ui,|ui| {
            egui::ScrollArea::vertical().show(ui,|ui| {
                if let Some(row)=rows.get(self.selected) {
                    ui.heading(format!("Observation #{}",self.selected+1));
                    for key in ["source","source_role","channel","t_ms","clock_basis","time_confidence","t_boot_us","wall_ms","anchor_unix_us","stale","subject"] {
                        ui.label(format!("{key}: {}",label(&row[key])));
                    }
                    ui.separator();
                    ui.label("Field values (source unit and declared meaning unit are distinct)");
                    if let Some(fields)=row["fields"].as_array() {
                        for field in fields {
                            ui.group(|ui| {
                                ui.label(RichText::new(format!("{} = {}",label(&field["name"]),label(&field["value"]))).strong());
                                for key in ["source_unit","meaning","unit","basis","disposition"] {
                                    ui.label(format!("{key}: {}",label(&field[key])));
                                }
                            });
                        }
                    }
                    ui.separator();
                    ui.label("Null and nonfinite text stay as recorded. No unit, UTC, calibration, cause or physical fault is inferred.");
                } else {
                    ui.heading("No observations in this report");
                    ui.label("The declared zero count is retained. Inspect source accounting below; no rows were invented.");
                }
            });
        });
        egui::CentralPanel::default().show(ui, |ui| {
            ui.heading(format!("{} saved observations", rows.len()));
            ui.label("Recorded file order · click a row");
            egui::ScrollArea::vertical().show_rows(ui, 62.0, rows.len(), |ui, range| {
                for index in range {
                    let row = &rows[index];
                    let title = format!(
                        "#{}  {} / {}\n{} ms | {}",
                        index + 1,
                        label(&row["source"]),
                        label(&row["channel"]),
                        label(&row["t_ms"]),
                        label(&row["clock_basis"])
                    );
                    if ui.selectable_label(self.selected == index, title).clicked() {
                        self.selected = index;
                    }
                }
            });
        });
    }
}
impl eframe::App for ReportApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.draw(ui);
    }
}
pub fn run(report: Report, screenshot: Option<PathBuf>) -> eframe::Result {
    eframe::run_native(
        "Musubi recorded observation viewer",
        native_options(),
        Box::new(move |cc| {
            cc.egui_ctx.set_theme(egui::ThemePreference::Dark);
            cc.egui_ctx.set_visuals(egui::Visuals::dark());
            Ok(Box::new(ReportApp::new(report, screenshot)))
        }),
    )
}

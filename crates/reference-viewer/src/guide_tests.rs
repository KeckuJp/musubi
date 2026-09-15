use super::*;
use musubi_reference_pipeline::export::{absences_csv, answer_text, claims_csv};
use musubi_reference_pipeline::load::Loaded;
use musubi_reference_pipeline::{Knowledge, ObservationWindow};

const STEPS: [(GuideSection, &str); 5] = [
    (GuideSection::Overview, "What this screen is — and is not"),
    (GuideSection::Summary, "What happened"),
    (GuideSection::Timeline, "What is missing, and since when"),
    (GuideSection::Causes, "Which failure pattern is consistent"),
    (GuideSection::Clocks, "How far to trust the clocks"),
];

fn fixture() -> ViewerApp {
    let knowledge = Knowledge::repo_public();
    let timeline =
        musubi_reference_scenario::generate(&musubi_reference_scenario::pre_demo_default(42));
    let out = musubi_reference_pipeline::run_pipeline_with(&timeline, &knowledge);
    let loaded = Loaded {
        files: out.files.clone(),
        window: ObservationWindow::Explicit {
            t0_ms: out.t0_ms,
            end_ms: out.end_ms,
        },
        t0_ms: Some(out.t0_ms),
        duration_ms: Some(out.end_ms - out.t0_ms),
        ground_truth: Vec::new(),
        skipped: Vec::new(),
        asset_family: out
            .assets
            .iter()
            .map(|asset| (asset.asset_id.clone(), asset.family))
            .collect(),
    };
    ViewerApp::new(
        Session {
            knowledge,
            loaded,
            out,
            metrics: None,
            input_dir: PathBuf::from("synthetic-guide-fixture"),
            profiles_root: PathBuf::from("profiles"),
        },
        None,
    )
}

fn frame(app: &mut ViewerApp, ctx: &egui::Context, events: Vec<egui::Event>) -> egui::FullOutput {
    let size = ctx
        .data(|data| data.get_temp::<egui::Vec2>(egui::Id::new("guide-test-screen-size")))
        .unwrap_or_else(|| vec2(1480.0, 940.0));
    let mut output = ctx.run_ui(
        egui::RawInput {
            screen_rect: Some(Rect::from_min_size(pos2(0.0, 0.0), size)),
            events,
            ..Default::default()
        },
        |ui| app.draw(ui),
    );
    output.textures_delta.clear();
    output
}

fn collect_text(shape: &egui::Shape, clip: Rect, texts: &mut Vec<(String, Rect)>) {
    match shape {
        egui::Shape::Vec(shapes) => {
            for shape in shapes {
                collect_text(shape, clip, texts);
            }
        }
        egui::Shape::Text(text) => {
            let rect = text.galley.rect.translate(text.pos.to_vec2());
            if rect.intersects(clip) {
                texts.push((text.galley.text().to_owned(), rect.intersect(clip)));
            }
        }
        _ => {}
    }
}

fn texts(output: &egui::FullOutput) -> Vec<(String, Rect)> {
    let mut texts = Vec::new();
    for shape in &output.shapes {
        collect_text(&shape.shape, shape.clip_rect, &mut texts);
    }
    texts
}

fn visible_text(output: &egui::FullOutput) -> String {
    texts(output)
        .into_iter()
        .map(|(text, _)| text)
        .collect::<Vec<_>>()
        .join("\n")
}

fn settled_frame(app: &mut ViewerApp, ctx: &egui::Context) -> egui::FullOutput {
    let mut output = frame(app, ctx, Vec::new());
    for _ in 0..24 {
        let next = frame(app, ctx, Vec::new());
        if texts(&output) == texts(&next) {
            return next;
        }
        output = next;
    }
    panic!("guide layout did not settle within 24 UI frames");
}

fn click_text(app: &mut ViewerApp, ctx: &egui::Context, label: &str) -> egui::FullOutput {
    let output = settled_frame(app, ctx);
    let point = texts(&output)
        .into_iter()
        .find(|(text, _)| text.contains(label))
        .unwrap_or_else(|| panic!("visible text not found: {label}"))
        .1
        .center();
    frame(
        app,
        ctx,
        vec![
            egui::Event::PointerMoved(point),
            egui::Event::PointerButton {
                pos: point,
                button: egui::PointerButton::Primary,
                pressed: true,
                modifiers: egui::Modifiers::NONE,
            },
        ],
    );
    frame(
        app,
        ctx,
        vec![egui::Event::PointerButton {
            pos: point,
            button: egui::PointerButton::Primary,
            pressed: false,
            modifiers: egui::Modifiers::NONE,
        }],
    )
}

#[test]
fn guide_starts_visible_with_five_descriptive_steps() {
    let mut app = fixture();
    assert!(app.guide_visible);
    assert_eq!(app.guide_section, GuideSection::Overview);
    let ctx = egui::Context::default();
    let output = settled_frame(&mut app, &ctx);
    let text = visible_text(&output);
    for (_, label) in STEPS {
        assert!(text.contains(label), "missing guide step: {label}");
    }
    assert!(text.contains("Hide guide"));
}

#[test]
fn selecting_each_section_requests_real_panel_focus() {
    let mut app = fixture();
    for (section, _) in STEPS {
        app.select_guide(section);
        assert_eq!(app.guide_section, section);
        assert_eq!(app.guide_requested, Some(section));
    }
    app.summary_open = false;
    app.select_guide(GuideSection::Summary);
    assert!(app.summary_open, "summary selection must reveal the answer");
}

#[test]
fn hide_show_preserves_selection_and_existing_timeline_state() {
    let mut app = fixture();
    app.select_guide(GuideSection::Clocks);
    app.selected = Some(app.s.out.claims.len() - 1);
    app.zoom_about(0.5, app.view_start);
    let before = (app.selected, app.view_start, app.view_end);
    app.toggle_guide();
    assert!(!app.guide_visible);
    assert_eq!(app.guide_section, GuideSection::Clocks);
    app.toggle_guide();
    assert!(app.guide_visible);
    assert_eq!(app.guide_section, GuideSection::Clocks);
    assert_eq!((app.selected, app.view_start, app.view_end), before);
}

#[test]
fn real_guide_buttons_select_sections_and_hide_show_reversibly() {
    let mut app = fixture();
    let ctx = egui::Context::default();
    frame(&mut app, &ctx, Vec::new());
    for (section, label) in STEPS {
        click_text(&mut app, &ctx, label);
        assert_eq!(app.guide_section, section, "button did not select {label}");
        let text = visible_text(&settled_frame(&mut app, &ctx));
        let expected = match section {
            GuideSection::Overview => NOTE_CEILING,
            GuideSection::Summary => QUESTION_7,
            GuideSection::Timeline => "ABSENT link_stats since",
            GuideSection::Causes => "telemetry_link_loss: heartbeat:heartbeat_stop",
            GuideSection::Clocks => &app.stand_in[0],
        };
        assert!(
            text.contains(expected),
            "guide selection must reveal existing panel content: {label}"
        );
    }
    click_text(&mut app, &ctx, "Hide guide");
    assert!(!app.guide_visible);
    let hidden = visible_text(&settled_frame(&mut app, &ctx));
    assert!(
        hidden.contains("Show guide"),
        "Show guide must stay visible"
    );
    assert!(
        !hidden.contains("READ IN THIS ORDER"),
        "hidden guide rail is still drawn"
    );
    click_text(&mut app, &ctx, "Show guide");
    assert!(app.guide_visible);
    assert_eq!(app.guide_section, GuideSection::Clocks);
}

#[test]
fn guide_modes_keep_state_legend_and_uncertainty_visible() {
    let mut app = fixture();
    let ctx = egui::Context::default();
    for visible in [true, false] {
        app.guide_visible = visible;
        let text = visible_text(&settled_frame(&mut app, &ctx));
        for term in ["OBS", "STALE", "ABSENT", "error band", "ORDER_UNKNOWN"] {
            assert!(text.contains(term), "missing {term} with guide={visible}");
        }
        assert!(
            text.contains(NOTE_CEILING),
            "claim ceiling must stay visible with guide={visible}"
        );
        assert!(text.contains("candidate"));
        assert!(text.contains("verdict"));
    }
}

#[test]
fn navigation_does_not_rewrite_summary_exports_or_pipeline_result() {
    let mut app = fixture();
    let exported = |app: &ViewerApp| {
        (
            answer_text(&app.s.out, &app.s.knowledge, app.s.metrics.as_ref()),
            absences_csv(&app.s.out),
            claims_csv(&app.s.out, &app.s.knowledge),
        )
    };
    let before = exported(&app);
    let original_answer = app.answer.clone();
    let original_result = format!("{:?}", app.s.out);
    let ctx = egui::Context::default();
    for (section, _) in STEPS {
        app.select_guide(section);
        frame(&mut app, &ctx, Vec::new());
        app.toggle_guide();
    }
    assert_eq!(app.answer, original_answer);
    assert_eq!(exported(&app), before);
    assert_eq!(format!("{:?}", app.s.out), original_result);
}

#[test]
fn overview_reports_loaded_files_with_no_entries_skipped() {
    let mut app = fixture();
    let ctx = egui::Context::default();
    let text = visible_text(&settled_frame(&mut app, &ctx));
    assert!(text.contains("Input status: 7 record files loaded · 0 entries not read"));
    assert!(!text.contains("Entries not read (0)"));
}

#[test]
fn unread_entry_details_preserve_loader_reasons_without_changing_results() {
    let mut app = fixture();
    app.s.loaded.skipped = vec![
        ("README.md".to_owned(), "not a record file".to_owned()),
        ("notes".to_owned(), "directory (not <asset>_dvr)".to_owned()),
    ];
    let unread_before = app.s.loaded.skipped.clone();
    let result_before = format!("{:?}", app.s.out);
    let answer_before = answer_text(&app.s.out, &app.s.knowledge, None);
    let ctx = egui::Context::default();
    let closed = visible_text(&settled_frame(&mut app, &ctx));
    assert!(closed.contains("Input status: 7 record files loaded · 2 entries not read"));
    assert!(closed.contains("Entries not read (2)"));
    assert!(!closed.contains("README.md"));
    click_text(&mut app, &ctx, "Entries not read (2)");
    let opened = visible_text(&settled_frame(&mut app, &ctx));
    for (name, reason) in &unread_before {
        assert!(opened.contains(name), "missing unread filename: {name}");
        assert!(
            opened.contains(reason),
            "missing exact loader reason: {reason}"
        );
    }
    assert_eq!(app.s.loaded.skipped, unread_before);
    assert_eq!(format!("{:?}", app.s.out), result_before);
    assert_eq!(
        answer_text(&app.s.out, &app.s.knowledge, None),
        answer_before
    );
}

#[test]
fn compact_window_keeps_guide_accessible_and_timeline_plot_usable() {
    let mut app = fixture();
    let ctx = egui::Context::default();
    ctx.data_mut(|data| {
        data.insert_temp(egui::Id::new("guide-test-screen-size"), vec2(900.0, 600.0));
    });
    for (section, label) in STEPS {
        click_text(&mut app, &ctx, label);
        assert_eq!(app.guide_section, section);
    }
    let output = settled_frame(&mut app, &ctx);
    let plot_width = output
        .shapes
        .iter()
        .find_map(|shape| match &shape.shape {
            egui::Shape::Text(text) if text.galley.text() == "09:00:00 +0.0s" => {
                Some(shape.clip_rect.width())
            }
            _ => None,
        })
        .expect("timeline must still draw its time axis at 900×600");
    assert!(
        plot_width >= 150.0,
        "timeline plot is too narrow at 900×600: {plot_width} points"
    );
    click_text(&mut app, &ctx, "Hide guide");
    assert!(!app.guide_visible);
    click_text(&mut app, &ctx, "Show guide");
    assert!(app.guide_visible);
    assert_eq!(app.guide_section, GuideSection::Clocks);
}

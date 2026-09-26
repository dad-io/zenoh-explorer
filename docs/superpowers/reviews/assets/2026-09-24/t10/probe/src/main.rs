//! T10 probe: checks colour and geometry claims against epaint/ecolor 0.29.1.
//! Scratch only; not part of the Zenoh Explorer build.
use epaint::{
    pos2, Color32, ClippedShape, Mesh, Pos2, Rect, Rgba, Shape, TessellationOptions, Tessellator,
};
use std::time::Instant;

fn hex(c: Color32) -> String {
    format!("#{:02x}{:02x}{:02x}", c.r(), c.g(), c.b())
}
fn h(s: &str) -> Color32 {
    let v = u32::from_str_radix(&s[1..], 16).unwrap();
    Color32::from_rgb((v >> 16) as u8, (v >> 8) as u8, v as u8)
}

// ---- Oklab (for comparing Chrome's likely keyframe interpolation space) ----
fn lin(u: u8) -> f32 {
    let c = u as f32 / 255.0;
    if c <= 0.04045 { c / 12.92 } else { ((c + 0.055) / 1.055).powf(2.4) }
}
fn gam(l: f32) -> f32 {
    let v = if l <= 0.0031308 { 12.92 * l } else { 1.055 * l.powf(1.0 / 2.4) - 0.055 };
    (v * 255.0).clamp(0.0, 255.0)
}
fn to_oklab(c: Color32) -> [f32; 3] {
    let (r, g, b) = (lin(c.r()), lin(c.g()), lin(c.b()));
    let l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b).cbrt();
    let m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b).cbrt();
    let s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b).cbrt();
    [
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    ]
}
fn from_oklab(o: [f32; 3]) -> [f32; 3] {
    let l = (o[0] + 0.3963377774 * o[1] + 0.2158037573 * o[2]).powi(3);
    let m = (o[0] - 0.1055613458 * o[1] - 0.0638541728 * o[2]).powi(3);
    let s = (o[0] - 0.0894841775 * o[1] - 1.2914855480 * o[2]).powi(3);
    [
        gam(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
        gam(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
        gam(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s),
    ]
}

// ---- CSS inset box-shadow profile ----
fn erf(x: f32) -> f32 {
    // Abramowitz-Stegun 7.1.26
    let t = 1.0 / (1.0 + 0.3275911 * x.abs());
    let y = 1.0
        - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
            + 0.254829592)
            * t
            * (-x * x).exp();
    if x >= 0.0 { y } else { -y }
}
fn phi(z: f32) -> f32 {
    0.5 * (1.0 + erf(z / std::f32::consts::SQRT_2))
}

/// One inset layer acting on one side: shade(d) = alpha * Phi((e - d) / sigma),
/// with e = offset*depth + spread (spread < 0 here) and sigma = blur/2 (CSS Backgrounds 3).
#[derive(Clone, Copy)]
struct Layer {
    name: &'static str,
    side: usize, // 0 top, 1 right, 2 bottom, 3 left
    offset: f32,
    blur: f32,
    spread: f32,
    alpha: f32,
}
const LAYERS: [Layer; 5] = [
    Layer { name: "1 top sidewall", side: 0, offset: 4.0, blur: 4.0, spread: -2.0, alpha: 0.65 },
    Layer { name: "2 left sidewall", side: 3, offset: 4.0, blur: 4.0, spread: -2.0, alpha: 0.65 },
    Layer { name: "3 bottom sidewall", side: 2, offset: 3.0, blur: 3.0, spread: -2.0, alpha: 0.55 },
    Layer { name: "4 right sidewall", side: 1, offset: 3.0, blur: 3.0, spread: -2.0, alpha: 0.55 },
    Layer { name: "5 contact (top)", side: 0, offset: 5.0, blur: 5.0, spread: -4.0, alpha: 0.40 },
];

fn shade(l: &Layer, depth: f32, d: f32) -> f32 {
    let e = l.offset * depth + l.spread;
    let sigma = l.blur / 2.0;
    l.alpha * phi((e - d) / sigma)
}

/// Premultiplied "over" in gamma space, same as egui_glow's blend (ONE, ONE_MINUS_SRC_ALPHA).
fn over(top: Color32, under: Color32) -> Color32 {
    let a = top.a() as f32 / 255.0;
    let f = |t: u8, u: u8| ((t as f32) + (u as f32) * (1.0 - a)).round().min(255.0) as u8;
    Color32::from_rgba_premultiplied(
        f(top.r(), under.r()),
        f(top.g(), under.g()),
        f(top.b(), under.b()),
        f(top.a(), under.a()),
    )
}
/// CSS premultiplies in gamma sRGB, and egui_glow blends in gamma space, so the matching
/// egui call is `gamma_multiply` on the opaque colour. `from_rgba_unmultiplied` premultiplies
/// in *linear* space (ecolor color32.rs:103-124) and composites lighter; T10_LINEAR=1 shows it.
static LINEAR: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
fn with_alpha(c: Color32, a: f32) -> Color32 {
    if LINEAR.load(std::sync::atomic::Ordering::Relaxed) {
        Color32::from_rgba_unmultiplied(c.r(), c.g(), c.b(), (a * 255.0).round() as u8)
    } else {
        Color32::from_rgb(c.r(), c.g(), c.b()).gamma_multiply(a)
    }
}

/// Per-vertex colour of the whole inset stack at point p inside `rect`.
fn stack_color(rect: Rect, p: Pos2, depth: [f32; 4], edge: [Color32; 4]) -> Color32 {
    let d = [p.y - rect.top(), rect.right() - p.x, rect.bottom() - p.y, p.x - rect.left()];
    let mut out = Color32::TRANSPARENT;
    // Layer 6: inner lower lip, 1pt hard band (painted under 1..5 per CSS list order).
    if d[2] < 1.0 {
        out = over(with_alpha(Color32::WHITE, 0x77 as f32 / 255.0), out);
    }
    for l in LAYERS.iter().rev() {
        let col = if l.name.starts_with('5') { h("#4e3c32") } else { edge[l.side] };
        let a = shade(l, depth[l.side], d[l.side]);
        out = over(with_alpha(col, a), out);
    }
    out
}

/// Ring-strip mesh: rings at inset distances `steps`, each a rounded rect sampled with a
/// fixed number of points per corner so consecutive rings triangulate 1:1.
fn ring_mesh(rect: Rect, radius: f32, steps: &[f32], depth: [f32; 4], edge: [Color32; 4]) -> Mesh {
    const PER_CORNER: usize = 6;
    let ring = |inset: f32| -> Vec<Pos2> {
        let r = rect.shrink(inset);
        let rad = (radius - inset).max(0.0);
        let centers = [
            (pos2(r.right() - rad, r.bottom() - rad), 0.0f32),
            (pos2(r.left() + rad, r.bottom() - rad), 1.0),
            (pos2(r.left() + rad, r.top() + rad), 2.0),
            (pos2(r.right() - rad, r.top() + rad), 3.0),
        ];
        // Break points on each straight side, BAND from the corners, so one side's shade
        // does not smear along the neighbouring side (vertex colours interpolate linearly).
        const BAND: f32 = 8.0;
        let bx = |from: f32, to: f32| {
            let d = (to - from).signum() * BAND.min((to - from).abs() / 2.0);
            [from + d, to - d]
        };
        let mut pts = Vec::with_capacity(4 * PER_CORNER + 8);
        for (k, (c, q)) in centers.into_iter().enumerate() {
            for i in 0..PER_CORNER {
                let t = (q + i as f32 / (PER_CORNER - 1) as f32) * std::f32::consts::FRAC_PI_2;
                pts.push(pos2(c.x + rad * t.cos(), c.y + rad * t.sin()));
            }
            match k {
                0 => bx(r.right(), r.left()).iter().for_each(|&x| pts.push(pos2(x, r.bottom()))),
                1 => bx(r.bottom(), r.top()).iter().for_each(|&y| pts.push(pos2(r.left(), y))),
                2 => bx(r.left(), r.right()).iter().for_each(|&x| pts.push(pos2(x, r.top()))),
                _ => bx(r.top(), r.bottom()).iter().for_each(|&y| pts.push(pos2(r.right(), y))),
            }
        }
        pts
    };
    let mut mesh = Mesh::default();
    let n = 4 * PER_CORNER + 8;
    for &s in steps {
        for p in ring(s) {
            mesh.colored_vertex(p, stack_color(rect, p, depth, edge));
        }
    }
    for j in 0..steps.len() - 1 {
        let (a, b) = ((j * n) as u32, ((j + 1) * n) as u32);
        for i in 0..n as u32 {
            let i2 = (i + 1) % n as u32;
            mesh.add_triangle(a + i, a + i2, b + i);
            mesh.add_triangle(a + i2, b + i2, b + i);
        }
    }
    mesh
}

fn main() {
    LINEAR.store(std::env::var("T10_LINEAR").is_ok(), std::sync::atomic::Ordering::Relaxed);
    let base = h("#d6d1c2");
    println!("== (f) seat(c) = 50% sRGB mix with #d6d1c2 ==");
    println!("input     lerp_to_gamma(0.5)   linear-Rgba lerp(0.5)   T9 Chrome hex");
    let t9 = ["#c8a48b", "#b2be99", "#d7c697", "#cdac86", "#bf9480"];
    for (i, s) in ["#ba7754", "#8eaa6f", "#d8ba6b", "#c4874a", "#a7563e"].iter().enumerate() {
        let c = h(s);
        let g = c.lerp_to_gamma(base, 0.5);
        let l: Color32 = {
            let (a, b) = (Rgba::from(c), Rgba::from(base));
            (a * 0.5 + b * 0.5).into()
        };
        println!("{s}   {}              {}                 {}  match={}", hex(g), hex(l), t9[i], hex(g) == t9[i]);
    }

    println!("\n== keyframe interpolation midpoints between consecutive stops (side k=0 path) ==");
    let seq = ["#c8a48b", "#b2be99", "#d7c697", "#cdac86", "#bf9480", "#b2be99", "#c8a48b"];
    let mut max_diff = 0.0f32;
    for w in seq.windows(2) {
        let (a, b) = (h(w[0]), h(w[1]));
        let s = a.lerp_to_gamma(b, 0.5);
        let (oa, ob) = (to_oklab(a), to_oklab(b));
        let o = from_oklab([(oa[0] + ob[0]) / 2.0, (oa[1] + ob[1]) / 2.0, (oa[2] + ob[2]) / 2.0]);
        let d = (0..3).map(|i| (o[i] - [s.r(), s.g(), s.b()][i] as f32).abs()).fold(0.0, f32::max);
        max_diff = max_diff.max(d);
        println!(
            "{} -> {}  srgb mid {}  oklab mid #{:02x}{:02x}{:02x}  max channel diff {:.1}",
            w[0], w[1], hex(s), o[0].round() as u8, o[1].round() as u8, o[2].round() as u8, d
        );
    }
    println!("max channel difference sRGB vs Oklab midpoint: {max_diff:.1}/255");

    println!("\n== (a) CSS inset layer profile: e = offset*depth + spread, sigma = blur/2 ==");
    println!("layer               depth  e(pt)  shade@0  shade@2  shade@4  depth where shade<1/255");
    for l in LAYERS.iter() {
        for depth in [0.0f32, 0.72, 1.0, 1.15] {
            let e = l.offset * depth + l.spread;
            let mut x = 0.0;
            while shade(l, depth, x) >= 1.0 / 255.0 && x < 20.0 {
                x += 0.25;
            }
            println!(
                "{:18}  {:5.2}  {:5.2}  {:6.3}   {:6.3}   {:6.3}   {:5.2}",
                l.name, depth, e, shade(l, depth, 0.0), shade(l, depth, 2.0), shade(l, depth, 4.0), x
            );
        }
    }

    println!("\n== frame-cost probe: ring mesh build + epaint 0.29.1 tessellation ==");
    let steps: Vec<f32> = (0..=16).map(|i| i as f32 * 0.5).collect(); // 0..8pt in 0.5pt rings
    let edge = [h("#b2be99"), h("#d7c697"), h("#cdac86"), h("#bf9480")];
    let surfaces: Vec<(Rect, f32)> = vec![
        (Rect::from_min_size(pos2(8.0, 60.0), epaint::vec2(90.0, 24.0)), 4.0), // source key
        (Rect::from_min_size(pos2(8.0, 90.0), epaint::vec2(1384.0, 30.0)), 0.0), // toolbar/header
        (Rect::from_min_size(pos2(8.0, 124.0), epaint::vec2(400.0, 760.0)), 0.0), // tree panel
        (Rect::from_min_size(pos2(412.0, 124.0), epaint::vec2(980.0, 760.0)), 0.0), // detail panel
        (Rect::from_min_size(pos2(420.0, 140.0), epaint::vec2(960.0, 200.0)), 6.0), // subpanel 1
        (Rect::from_min_size(pos2(420.0, 350.0), epaint::vec2(960.0, 200.0)), 6.0), // subpanel 2
        (Rect::from_min_size(pos2(420.0, 560.0), epaint::vec2(960.0, 200.0)), 6.0), // subpanel 3
        (Rect::from_min_size(pos2(420.0, 770.0), epaint::vec2(960.0, 100.0)), 6.0), // subpanel 4
    ];
    let iters = 2000;
    let t0 = Instant::now();
    let mut verts = 0;
    let mut tris = 0;
    let mut prims = 0;
    for _ in 0..iters {
        let shapes: Vec<ClippedShape> = surfaces
            .iter()
            .map(|(r, rad)| {
                let m = ring_mesh(*r, *rad, &steps, [1.15, 1.0, 1.12, 0.88], edge);
                verts = m.vertices.len();
                tris = m.indices.len() / 3;
                ClippedShape { clip_rect: Rect::EVERYTHING, shape: Shape::mesh(m) }
            })
            .collect();
        let mut t = Tessellator::new(2.0, TessellationOptions::default(), [2048, 64], vec![]);
        prims = t.tessellate_shapes(shapes).len();
    }
    let elapsed = t0.elapsed();
    // Probe image: a 60x30 key (radius 6) and a 60x40 square panel (points, drawn at 3x) at depths
    // pending 0.72, rest 1.0 (orange), peak 1.15 (spectrum), on the Snow White panel colour.
    let (w, hh, sc) = (3 * 230usize, 2 * 150usize, 3.0f32);
    let mut img = vec![[0xf3 as f32 / 255.0, 0xf0 as f32 / 255.0, 0xe7 as f32 / 255.0]; w * hh];
    let orange = h("#c8a48b");
    let states = [(0.72f32, [orange; 4]), (1.0, [orange; 4]), (1.15, edge)];
    for (i, (d, e)) in states.iter().enumerate() {
        let ox = 10.0 + i as f32 * 75.0;
        let key = Rect::from_min_size(pos2(ox, 8.0), epaint::vec2(60.0, 30.0));
        let pan = Rect::from_min_size(pos2(ox, 50.0), epaint::vec2(60.0, 40.0));
        let mut face = Mesh::default();
        face.add_colored_rect(key, h("#e7e3d7"));
        face.add_colored_rect(pan, h("#f3f0e7"));
        raster(&face, w, hh, sc, &mut img);
        raster(&ring_mesh(key, 6.0, &steps, [*d; 4], *e), w, hh, sc, &mut img);
        raster(&ring_mesh(pan, 0.0, &steps, [*d; 4], *e), w, hh, sc, &mut img);
    }
    let mut ppm = format!("P6 {w} {hh} 255\n").into_bytes();
    for p in &img { for c in p { ppm.push((c.clamp(0.0, 1.0) * 255.0).round() as u8); } }
    let out = if LINEAR.load(std::sync::atomic::Ordering::Relaxed) { "bevel-probe-linear-premult.ppm" } else { "bevel-probe.ppm" };
    std::fs::write(out, ppm).unwrap();
    println!("wrote {out} ({w}x{hh}, 3x): pending 0.72 | rest 1.0 | peak 1.15 spectrum");

    let per = elapsed.as_secs_f64() * 1e6 / iters as f64;
    println!(
        "{} surfaces, {} vertices / {} triangles each; build+tessellate = {:.1} us per frame (release, 1 thread); {} primitive(s)",
        surfaces.len(), verts, tris, per, prims
    );
}

/// Software rasteriser for the probe image only: gamma-space barycentric interpolation and
/// premultiplied "over", i.e. what egui_glow does (v_rgba_in_gamma, ONE/ONE_MINUS_SRC_ALPHA).
pub fn raster(mesh: &Mesh, w: usize, hgt: usize, scale: f32, bg: &mut Vec<[f32; 3]>) {
    let v = &mesh.vertices;
    for t in mesh.indices.chunks(3) {
        let (a, b, c) = (v[t[0] as usize], v[t[1] as usize], v[t[2] as usize]);
        let (ax, ay, bx, by, cx, cy) = (a.pos.x * scale, a.pos.y * scale, b.pos.x * scale, b.pos.y * scale, c.pos.x * scale, c.pos.y * scale);
        let area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
        if area.abs() < 1e-6 { continue; }
        let (x0, x1) = (ax.min(bx).min(cx).floor().max(0.0) as usize, (ax.max(bx).max(cx).ceil() as usize).min(w));
        let (y0, y1) = (ay.min(by).min(cy).floor().max(0.0) as usize, (ay.max(by).max(cy).ceil() as usize).min(hgt));
        for y in y0..y1 { for x in x0..x1 {
            let (px, py) = (x as f32 + 0.5013, y as f32 + 0.5031); // nudge off exact ring edges
            let w0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) / area;
            let w1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) / area;
            let w2 = 1.0 - w0 - w1;
            if w0 < 0.0 || w1 < 0.0 || w2 < 0.0 { continue; }
            let ch = |f: fn(&Color32) -> u8| (w0 * f(&a.color) as f32 + w1 * f(&b.color) as f32 + w2 * f(&c.color) as f32) / 255.0;
            let (r, g, bb, al) = (ch(Color32::r), ch(Color32::g), ch(Color32::b), ch(Color32::a));
            let p = &mut bg[y * w + x];
            *p = [r + p[0] * (1.0 - al), g + p[1] * (1.0 - al), bb + p[2] * (1.0 - al)];
        }}
    }
}

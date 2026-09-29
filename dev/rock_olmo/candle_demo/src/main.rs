//! spectra-demo: OLMo 2 1B base vs the v3l spectra fine-tune, quantized, on candle.
//!
//!   spectra-demo quantize --src <hf checkpoint dir> --out <file.gguf> [--dtype q6k|q8_0|f16|f32]
//!   spectra-demo serve --model base=<gguf> --model v3l=<gguf> [--port 8080] [--static static]
//!                      [--device auto|cpu|metal|cuda] [--host 127.0.0.1]
//!   spectra-demo run --model <gguf> --probes <json> --out <json> [--use-stops]
//!   spectra-demo check-tokens --model <gguf> --probes <json>
//!
//! A quantized file is self-contained: the checkpoint's config.json and tokenizer.json travel
//! as GGUF metadata, so the laptop needs only the two .gguf files and the static/ folder.
//! Decoding is greedy, as the probes ran; generation stops at <|endoftext|>, at `max_new`,
//! or (serve) at the first stop string.

mod qolmo2;

use anyhow::{anyhow, bail, Context, Result};
use candle_core::quantized::{gguf_file, GgmlDType, QTensor};
use candle_core::{DType, Device};
use candle_transformers::quantized_var_builder::VarBuilder;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::Instant;
use tokenizers::Tokenizer;

const EOS: u32 = 100257; // <|endoftext|>
const MAX_NEW_CAP: usize = 128;

// ── arguments ────────────────────────────────────────────────────────
struct Args {
    cmd: String,
    kv: BTreeMap<String, Vec<String>>,
    flags: Vec<String>,
}

impl Args {
    fn parse() -> Result<Self> {
        let mut it = std::env::args().skip(1);
        let cmd = it.next().ok_or_else(|| anyhow!("usage: spectra-demo quantize|serve|run|check-tokens ..."))?;
        let (mut kv, mut flags) = (BTreeMap::<String, Vec<String>>::new(), Vec::new());
        let rest: Vec<String> = it.collect();
        let mut i = 0;
        while i < rest.len() {
            let k = rest[i].trim_start_matches("--").to_string();
            if rest.get(i + 1).map_or(true, |v| v.starts_with("--")) {
                flags.push(k);
                i += 1;
            } else {
                kv.entry(k).or_default().push(rest[i + 1].clone());
                i += 2;
            }
        }
        Ok(Self { cmd, kv, flags })
    }
    fn one(&self, k: &str) -> Option<&str> {
        self.kv.get(k).and_then(|v| v.last()).map(|s| s.as_str())
    }
    fn req(&self, k: &str) -> Result<&str> {
        self.one(k).ok_or_else(|| anyhow!("missing --{k}"))
    }
}

fn device(want: &str) -> Result<Device> {
    Ok(match want {
        "cpu" => Device::Cpu,
        "cuda" => Device::new_cuda(0)?,
        "metal" => Device::new_metal(0)?,
        "auto" if candle_core::utils::cuda_is_available() => Device::new_cuda(0)?,
        "auto" if candle_core::utils::metal_is_available() => Device::new_metal(0)?,
        "auto" => Device::Cpu,
        other => bail!("unknown device {other:?} (auto, cpu, metal, cuda)"),
    })
}

fn device_name(d: &Device) -> &'static str {
    match d {
        Device::Cpu => "cpu",
        Device::Cuda(_) => "cuda",
        Device::Metal(_) => "metal",
    }
}

// ── quantize ─────────────────────────────────────────────────────────
fn quantize(a: &Args) -> Result<()> {
    let src = PathBuf::from(a.req("src")?);
    let out = PathBuf::from(a.req("out")?);
    let qtype = match a.one("dtype").unwrap_or("q6k") {
        "q6k" => GgmlDType::Q6K,
        "q8_0" => GgmlDType::Q8_0,
        "f16" => GgmlDType::F16,
        "f32" => GgmlDType::F32, // reference only: isolates quantization loss from the runtime
        other => bail!("--dtype {other}: use q6k, q8_0, f16 or f32"),
    };
    let config = std::fs::read_to_string(src.join("config.json"))?;
    let tokenizer = std::fs::read_to_string(src.join("tokenizer.json"))?;
    qolmo2::Config::from_hf_json(&config)?; // fail early on a config the loader cannot read
    let mut files: Vec<PathBuf> = std::fs::read_dir(&src)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "safetensors"))
        .collect();
    files.sort();
    anyhow::ensure!(!files.is_empty(), "no .safetensors in {}", src.display());
    let st = unsafe { candle_core::safetensors::MmapedSafetensors::multi(&files)? };
    let mut names: Vec<String> = st.tensors().into_iter().map(|(n, _)| n).collect();
    names.sort();
    let t0 = Instant::now();
    let mut qs: Vec<(String, QTensor)> = Vec::with_capacity(names.len());
    let (mut n_q, mut n_f) = (0, 0);
    for name in &names {
        let t = st.load(name, &Device::Cpu)?.to_dtype(DType::F32)?;
        // Matrices are quantized (their rows are multiples of the 256-value k-quant block);
        // the norm vectors stay f32.
        let q = if t.rank() == 2 && t.dim(1)? % 256 == 0 {
            n_q += 1;
            QTensor::quantize(&t, qtype)?
        } else {
            n_f += 1;
            QTensor::quantize(&t, GgmlDType::F32)?
        };
        qs.push((name.clone(), q));
    }
    let meta: Vec<(&str, gguf_file::Value)> = vec![
        ("general.architecture", gguf_file::Value::String("olmo2-hf-names".into())),
        ("demo.source", gguf_file::Value::String(src.display().to_string())),
        ("demo.quant", gguf_file::Value::String(format!("{qtype:?}"))),
        ("demo.config_json", gguf_file::Value::String(config)),
        ("demo.tokenizer_json", gguf_file::Value::String(tokenizer)),
    ];
    let meta_refs: Vec<(&str, &gguf_file::Value)> = meta.iter().map(|(k, v)| (*k, v)).collect();
    let q_refs: Vec<(&str, &QTensor)> = qs.iter().map(|(k, v)| (k.as_str(), v)).collect();
    let mut f = std::fs::File::create(&out)?;
    gguf_file::write(&mut f, &meta_refs, &q_refs)?;
    let mb = std::fs::metadata(&out)?.len() as f64 / 1e6;
    println!(
        "{} -> {} ({:?}): {n_q} quantized + {n_f} f32 tensors, {mb:.0} MB, {:.1}s",
        src.display(),
        out.display(),
        qtype,
        t0.elapsed().as_secs_f64()
    );
    Ok(())
}

// ── model + generation ───────────────────────────────────────────────
struct Loaded {
    model: qolmo2::Model,
    tok: Tokenizer,
    quant: String,
    file: String,
    size_mb: f64,
}

fn load(path: &Path, dev: &Device) -> Result<Loaded> {
    let mut f = std::fs::File::open(path).with_context(|| path.display().to_string())?;
    let content = gguf_file::Content::read(&mut f)?;
    let text = |k: &str| -> Result<String> {
        Ok(content.metadata.get(k).ok_or_else(|| anyhow!("{}: no {k} (not made by spectra-demo quantize?)", path.display()))?.to_string()?.clone())
    };
    let cfg = qolmo2::Config::from_hf_json(&text("demo.config_json")?)?;
    let tok = Tokenizer::from_bytes(text("demo.tokenizer_json")?.as_bytes()).map_err(|e| anyhow!("tokenizer: {e}"))?;
    let quant = text("demo.quant")?;
    drop(f);
    let model = qolmo2::Model::new(&cfg, VarBuilder::from_gguf(path, dev)?)?;
    let size_mb = std::fs::metadata(path)?.len() as f64 / 1e6;
    Ok(Loaded { model, tok, quant, file: path.display().to_string(), size_mb })
}

#[derive(Serialize)]
struct Gen {
    text: String,
    n_prompt: usize,
    n_gen: usize,
    ms_prefill: f64,
    ms_total: f64,
    stop: String,
}

fn encode(m: &Loaded, prompt: &str) -> Result<Vec<u32>> {
    Ok(m.tok.encode(prompt, true).map_err(|e| anyhow!("encode: {e}"))?.get_ids().to_vec())
}

fn generate(m: &mut Loaded, prompt: &str, max_new: usize, stops: &[String]) -> Result<Gen> {
    let ids = encode(m, prompt)?;
    let t0 = Instant::now();
    m.model.clear_cache();
    let mut next = qolmo2::argmax(&m.model.forward(&ids, 0)?)?;
    let ms_prefill = t0.elapsed().as_secs_f64() * 1e3;
    let mut out: Vec<u32> = Vec::new();
    let mut stop = "max_new".to_string();
    let mut text = String::new();
    while out.len() < max_new {
        if next == EOS {
            stop = "eos".into();
            break;
        }
        out.push(next);
        text = m.tok.decode(&out, true).map_err(|e| anyhow!("decode: {e}"))?;
        if let Some(s) = stops.iter().find(|s| text.contains(s.as_str())) {
            stop = format!("stop {s:?}");
            break;
        }
        if out.len() == max_new {
            break;
        }
        next = qolmo2::argmax(&m.model.forward(&[next], ids.len() + out.len() - 1)?)?;
    }
    Ok(Gen {
        text,
        n_prompt: ids.len(),
        n_gen: out.len(),
        ms_prefill,
        ms_total: t0.elapsed().as_secs_f64() * 1e3,
        stop,
    })
}

#[derive(Deserialize)]
struct Probe {
    id: String,
    prompt: String,
    #[serde(default)]
    ids: Vec<u32>,
    max_new: usize,
    #[serde(default)]
    stop: Vec<String>,
}

#[derive(Deserialize)]
struct ProbeFile {
    probes: Vec<Probe>,
}

fn read_probes(path: &str) -> Result<Vec<Probe>> {
    Ok(serde_json::from_str::<ProbeFile>(&std::fs::read_to_string(path)?)?.probes)
}

// ── run / check-tokens ───────────────────────────────────────────────
fn run(a: &Args) -> Result<()> {
    let dev = device(a.one("device").unwrap_or("auto"))?;
    let mut m = load(Path::new(a.req("model")?), &dev)?;
    let probes = read_probes(a.req("probes")?)?;
    let use_stops = a.flags.iter().any(|f| f == "use-stops");
    let t0 = Instant::now();
    let mut rows = Vec::new();
    for p in &probes {
        let stops: &[String] = if use_stops { &p.stop } else { &[] };
        let g = generate(&mut m, &p.prompt, p.max_new, stops)?;
        rows.push(serde_json::json!({"id": p.id, "gen": g}));
    }
    let doc = serde_json::json!({"model": m.file, "quant": m.quant, "device": device_name(&dev), "stops": use_stops,
                                 "seconds": t0.elapsed().as_secs_f64(), "rows": rows});
    std::fs::write(a.req("out")?, serde_json::to_string_pretty(&doc)?)?;
    println!("{} probes in {:.1}s on {} -> {}", probes.len(), t0.elapsed().as_secs_f64(), device_name(&dev), a.req("out")?);
    Ok(())
}

fn check_tokens(a: &Args) -> Result<()> {
    let m = load(Path::new(a.req("model")?), &Device::Cpu)?;
    let probes = read_probes(a.req("probes")?)?;
    let bad: Vec<&str> = probes.iter().filter(|p| encode(&m, &p.prompt).map_or(true, |ids| ids != p.ids)).map(|p| p.id.as_str()).collect();
    println!("{}/{} prompts tokenize exactly as the Python probes did", probes.len() - bad.len(), probes.len());
    anyhow::ensure!(bad.is_empty(), "mismatched: {bad:?}");
    Ok(())
}

// ── serve ────────────────────────────────────────────────────────────
#[derive(Deserialize)]
struct GenReq {
    model: String,
    prompt: String,
    #[serde(default = "default_max_new")]
    max_new: usize,
    #[serde(default)]
    stop: Vec<String>,
}

fn default_max_new() -> usize {
    32
}

fn content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|x| x.to_str()) {
        Some("html") => "text/html; charset=utf-8",
        Some("json") => "application/json",
        Some("js") => "text/javascript",
        Some("css") => "text/css",
        Some("svg") => "image/svg+xml",
        _ => "application/octet-stream",
    }
}

fn respond(req: tiny_http::Request, code: u16, ctype: &str, body: Vec<u8>) {
    let h = |k: &str, v: &str| tiny_http::Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap();
    let r = tiny_http::Response::from_data(body)
        .with_status_code(code)
        .with_header(h("Content-Type", ctype))
        .with_header(h("Access-Control-Allow-Origin", "*"))
        .with_header(h("Access-Control-Allow-Headers", "Content-Type"))
        .with_header(h("Cache-Control", "no-store"));
    let _ = req.respond(r);
}

fn serve(a: &Args) -> Result<()> {
    let dev = device(a.one("device").unwrap_or("auto"))?;
    let static_dir = PathBuf::from(a.one("static").unwrap_or("static"));
    let mut models: BTreeMap<String, Loaded> = BTreeMap::new();
    for spec in a.kv.get("model").ok_or_else(|| anyhow!("give at least one --model key=file.gguf"))? {
        let (k, p) = spec.split_once('=').ok_or_else(|| anyhow!("--model wants key=file.gguf, got {spec}"))?;
        let t0 = Instant::now();
        let m = load(Path::new(p), &dev)?;
        println!("loaded {k} <- {p} ({}, {:.0} MB) in {:.1}s", m.quant, m.size_mb, t0.elapsed().as_secs_f64());
        models.insert(k.to_string(), m);
    }
    // Warm-up: the first forward on a device allocates and compiles kernels.
    for m in models.values_mut() {
        generate(m, "<mineral species=\"", 1, &[])?;
    }
    let addr = format!("{}:{}", a.one("host").unwrap_or("127.0.0.1"), a.one("port").unwrap_or("8080"));
    let server = tiny_http::Server::http(&addr).map_err(|e| anyhow!("bind {addr}: {e}"))?;
    println!("serving on http://{addr}/ (device {}, static {})", device_name(&dev), static_dir.display());
    for mut req in server.incoming_requests() {
        let url = req.url().split('?').next().unwrap_or("/").to_string();
        match (req.method().clone(), url.as_str()) {
            (tiny_http::Method::Options, _) => respond(req, 204, "text/plain", vec![]),
            (tiny_http::Method::Get, "/api/health") => {
                let ms: BTreeMap<&String, serde_json::Value> = models
                    .iter()
                    .map(|(k, m)| (k, serde_json::json!({"quant": m.quant, "file": m.file, "size_mb": m.size_mb})))
                    .collect();
                let body = serde_json::json!({"device": device_name(&dev), "models": ms});
                respond(req, 200, "application/json", body.to_string().into_bytes());
            }
            (tiny_http::Method::Post, "/api/generate") => {
                let mut body = String::new();
                let _ = req.as_reader().read_to_string(&mut body);
                let result = serde_json::from_str::<GenReq>(&body).map_err(anyhow::Error::from).and_then(|g| {
                    let m = models.get_mut(&g.model).ok_or_else(|| anyhow!("no model {:?}", g.model))?;
                    generate(m, &g.prompt, g.max_new.min(MAX_NEW_CAP), &g.stop)
                });
                match result {
                    Ok(g) => respond(req, 200, "application/json", serde_json::to_vec(&g)?),
                    Err(e) => respond(req, 400, "application/json", serde_json::json!({"error": e.to_string()}).to_string().into_bytes()),
                }
            }
            (tiny_http::Method::Get, path) => {
                let name = if path == "/" { "index.html" } else { path.trim_start_matches('/') };
                let ok = !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || "._-".contains(c)) && !name.starts_with('.');
                let file = static_dir.join(name);
                match (ok, std::fs::read(&file)) {
                    (true, Ok(bytes)) => respond(req, 200, content_type(&file), bytes),
                    _ => respond(req, 404, "text/plain", b"not found".to_vec()),
                }
            }
            _ => respond(req, 405, "text/plain", b"method not allowed".to_vec()),
        }
    }
    Ok(())
}

fn main() -> Result<()> {
    let a = Args::parse()?;
    match a.cmd.as_str() {
        "quantize" => quantize(&a),
        "serve" => serve(&a),
        "run" => run(&a),
        "check-tokens" => check_tokens(&a),
        other => bail!("unknown command {other:?}: quantize | serve | run | check-tokens"),
    }
}
